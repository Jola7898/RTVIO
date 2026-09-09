"""
Three clock domains -> one session-relative timeline.

    frames     wall clock, ms   (System.currentTimeMillis)      epoch
    gps        wall clock, ms   (Location.getTime)              epoch
    imu        MONOTONIC, ns    (SensorEvent.timestamp)         boot
    pipeline   float seconds                                    0.0 at session start

The IMU split is deliberate on the app side and worth keeping: a monotonic
clock does not jump when NTP corrects the wall clock mid-flight. The cost
is that boot-ns and epoch-ms have no defined relationship, and subtracting
them naively yields ~1e12 - which is a useful "you skipped this file"
alarm, not a calibration.

WHAT THIS DOES

A short warm-up window buffers the opening packets, estimates the offset
between the two domains, freezes it, and then converts everything through
that one constant. Estimation is a MINIMUM FILTER over receive times
rather than the obvious "pair the first frame with the first IMU sample":

    for each packet:  observed_lag = t_received - t_device
    estimate          min(observed_lag) over the warm-up window

Transport delay is strictly positive and jittery, so the minimum of many
observations is a far better estimate of the constant part than any single
pairing - the first-packet pairing is accurate only to one inter-packet
interval (tens of ms), and it happens to be the noisiest sample available.

WHAT THIS DOES NOT DO, AND WHY YOU SHOULD CARE

The minimum filter still absorbs each stream's own capture-to-send
latency, and those differ: an IMU batch is 32 bytes per sample and leaves
almost immediately, while a ~200 KB JPEG must be encoded and then
serialised. On a 5 Mbps link that serialisation alone is ~300 ms. Frame
timestamps are stamped at capture, so most of it is common-mode and
cancels, but not all of it, and the residual is a systematic camera-vs-IMU
misalignment.

Tens of ms of misalignment at walking pace is centimetres of position
error injected into every frame, and it surfaces downstream as scale and
drift error that no amount of noise tuning removes.

The correct fix is to estimate the offset as an ONLINE EKF PARAMETER
(standard VIO practice). `imu_time_offset_s` below is a single mutable
number that every conversion reads through, precisely so that change is a
filter change and not a rewrite of this module. Until then, treat absolute
accuracy claims with suspicion.

The cleaner fix still, if you control both ends, is a new packet type
carrying System.currentTimeMillis() and SystemClock.elapsedRealtimeNanos()
read back to back. That is a protocol change spanning Protocol.kt,
StreamClient.kt and every receiver; it is not done.
"""
import time

WARMUP_S = 0.75           # stream time spent estimating the offset before
                          # anything is emitted; ~1 GPS interval, ~20 frames
WARMUP_MIN_FRAMES = 3
WARMUP_MIN_IMU = 20

# Beyond this the two domains disagree so badly that the offset estimate is
# certainly wrong rather than merely imprecise, and proceeding would produce
# a smoothly incorrect reconstruction instead of an error.
MAX_PLAUSIBLE_SKEW_S = 5.0


class ClockNotReady(Exception):
    """Conversion attempted before the warm-up window closed."""


class _Span:
    """Min/max of a stream of values, and the distance between them."""

    __slots__ = ("lo", "hi")

    def __init__(self):
        self.lo = None
        self.hi = None

    def observe(self, v):
        self.lo = v if self.lo is None else min(self.lo, v)
        self.hi = v if self.hi is None else max(self.hi, v)

    @property
    def width(self):
        return 0.0 if self.lo is None else self.hi - self.lo


class SessionClock:
    """Maps both device clock domains onto session-relative seconds.

    Not thread-safe by itself; source.py calls it only from the single
    reader thread that owns the socket.
    """

    def __init__(self, warmup_s=WARMUP_S, now=time.monotonic):
        self._warmup_s = warmup_s
        self._now = now

        self._first_recv = None
        self._min_lag_wall = None      # min(t_recv - wall_s)
        self._min_lag_boot = None      # min(t_recv - boot_s)
        self._n_frames = 0
        self._n_imu = 0
        # Warm-up progress is measured in DEVICE time, not receiver time. The
        # two decouple whenever packets arrive in a burst - a faster-than-real-
        # time replay, or the flush after a reconnect - and a receiver-clock
        # criterion then never fires, so the session ends still warming up and
        # reconstructs nothing. Device-time span is what "we have seen enough
        # of the capture to estimate the offset" actually means.
        self._wall_span = _Span()
        self._boot_span = _Span()

        self.ready = False
        # boot_s + boot_to_wall_s == the same instant on the wall clock.
        self.boot_to_wall_s = None
        # Session zero, in wall-clock seconds. Frozen at warm-up close.
        self.wall_t0_s = None
        # Additive correction on the IMU timeline only, in seconds. Positive
        # means IMU samples are treated as later than their raw conversion
        # says. Intended to become an EKF state; see the module docstring.
        self.imu_time_offset_s = 0.0

        # Diagnostics, reported at warm-up close and worth logging.
        self.warmup_frames = 0
        self.warmup_imu = 0
        self.raw_domain_gap_s = None

    # ------------------------------------------------------------ observe --

    def observe_wall(self, timestamp_ms, t_recv=None):
        """Record a wall-clock-stamped packet (frame or GPS)."""
        t_recv = self._now() if t_recv is None else t_recv
        self._note_recv(t_recv)
        self._wall_span.observe(timestamp_ms / 1e3)
        lag = t_recv - timestamp_ms / 1e3
        if self._min_lag_wall is None or lag < self._min_lag_wall:
            self._min_lag_wall = lag

    def observe_boot(self, t_ns, t_recv=None):
        """Record a monotonic-since-boot-stamped packet (IMU)."""
        t_recv = self._now() if t_recv is None else t_recv
        self._note_recv(t_recv)
        self._boot_span.observe(t_ns / 1e9)
        lag = t_recv - t_ns / 1e9
        if self._min_lag_boot is None or lag < self._min_lag_boot:
            self._min_lag_boot = lag

    def observe_frame(self, pkt, t_recv=None):
        self._n_frames += 1
        self.observe_wall(pkt.timestamp_ms, t_recv)

    def observe_imu(self, samples, t_recv=None):
        if not samples:
            return
        self._n_imu += len(samples)
        # Stamping every sample in the batch with one receive time would bias
        # the estimate by half the batch duration. The newest sample is the
        # one whose device time is genuinely closest to this receive time, so
        # it is the only honest observation in the batch.
        self.observe_boot(samples[-1].t_ns, t_recv)

    def observe_gps(self, pkt, t_recv=None):
        self.observe_wall(pkt.timestamp_ms, t_recv)

    def _note_recv(self, t_recv):
        if self._first_recv is None:
            self._first_recv = t_recv

    # -------------------------------------------------------------- close --

    def warmup_elapsed_s(self):
        """How much CAPTURE has been seen, in device seconds. Deliberately not
        receiver wall time - see the _wall_span comment in __init__."""
        return min(self._wall_span.width, self._boot_span.width)

    def can_close(self):
        return (self._min_lag_wall is not None
                and self._min_lag_boot is not None
                and self._n_frames >= WARMUP_MIN_FRAMES
                and self._n_imu >= WARMUP_MIN_IMU
                and self.warmup_elapsed_s() >= self._warmup_s)

    def force_close(self):
        """Close on whatever has been seen. Used when the stream ends mid
        warm-up: a very short session is still worth reconstructing, and the
        offset estimate from a handful of packets is imprecise rather than
        meaningless. Returns False if not even one packet of each kind
        arrived, in which case there is genuinely no timeline to build."""
        if self._min_lag_wall is None or self._min_lag_boot is None:
            return False
        self.close_warmup()
        return True

    def close_warmup(self, session_zero_wall_s=None):
        """Freeze the offset and session zero. Idempotent."""
        if self.ready:
            return
        if self._min_lag_wall is None or self._min_lag_boot is None:
            raise ClockNotReady(
                "need at least one wall-stamped and one boot-stamped packet; "
                "have frames=%d imu=%d" % (self._n_frames, self._n_imu))

        # lag_wall = latency - W and lag_boot = latency - B, so their
        # difference is B - W: the amount that must be ADDED to a boot-domain
        # second to express the same instant on the wall clock.
        self.boot_to_wall_s = self._min_lag_boot - self._min_lag_wall
        self.wall_t0_s = (session_zero_wall_s if session_zero_wall_s is not None
                          else self._first_recv - self._min_lag_wall)
        self.warmup_frames = self._n_frames
        self.warmup_imu = self._n_imu
        self.ready = True

    # ------------------------------------------------------------ convert --

    def _require(self):
        if not self.ready:
            raise ClockNotReady("close_warmup() has not run; no offset is known yet")

    def wall_ms_to_session_s(self, timestamp_ms):
        self._require()
        return timestamp_ms / 1e3 - self.wall_t0_s

    def boot_ns_to_session_s(self, t_ns):
        self._require()
        return (t_ns / 1e9 + self.boot_to_wall_s + self.imu_time_offset_s
                - self.wall_t0_s)

    # -------------------------------------------------------- diagnostics --

    def check_domains_overlap(self, wall_span_s, boot_span_s):
        """Acceptance test from INTEGRATION.md section 8: after conversion the
        two domains must describe the same interval. A ~1e12 disagreement
        means the conversion was skipped somewhere; anything past
        MAX_PLAUSIBLE_SKEW_S means the offset estimate is wrong rather than
        imprecise. Returns (ok, message)."""
        (w_lo, w_hi), (b_lo, b_hi) = wall_span_s, boot_span_s
        self.raw_domain_gap_s = max(w_lo - b_hi, b_lo - w_hi, 0.0)
        overlap = min(w_hi, b_hi) - max(w_lo, b_lo)
        if overlap > 0 and self.raw_domain_gap_s == 0.0:
            return True, ("domains overlap by %.2fs (wall %.2f..%.2f, imu %.2f..%.2f)"
                          % (overlap, w_lo, w_hi, b_lo, b_hi))
        if self.raw_domain_gap_s > 1e6:
            return False, ("domains are %.3es apart - the boot/epoch conversion was "
                           "skipped entirely (INTEGRATION.md section 4.1)"
                           % self.raw_domain_gap_s)
        return False, ("domains do not overlap; gap %.3fs (wall %.2f..%.2f, imu %.2f..%.2f)"
                       % (self.raw_domain_gap_s, w_lo, w_hi, b_lo, b_hi))

    def describe(self):
        if not self.ready:
            return "SessionClock(warming up)"
        return ("SessionClock(boot->wall %+.6fs, t0 %.3f epoch-s, "
                "imu offset %+.1fms, warm-up %d frames / %d imu)"
                % (self.boot_to_wall_s, self.wall_t0_s,
                   self.imu_time_offset_s * 1e3, self.warmup_frames, self.warmup_imu))
