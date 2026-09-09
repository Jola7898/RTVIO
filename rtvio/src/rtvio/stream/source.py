"""
Packet sources and the fan-out that feeds the live pipeline.

    SocketPacketSource   the phone, over TCP
    ReplayPacketSource   a recorded session, re-emitted at its own pace
    StreamSession        clock alignment + reordering + fan-out

THE STRUCTURAL RULE THIS FILE ENFORCES

StreamSession hands every packet to a list of subscribers. The live
reconstruction is one subscriber; the fixture recorder is another. They
see identical events and neither can see the other. Deleting the recorder
cannot change the model, because the model never learns where a packet
came from - and the reconstruction consumer has no file-reading code path
at all.

ReplayPacketSource exists for exactly one purpose: reproducing a bug
deterministically. It is a test harness, not a data path. A model built
from a replay is a model built for debugging.

WHY THERE IS A REORDER BUFFER

Packets do not arrive in timestamp order, and the reason is structural
rather than incidental: a frame is stamped at capture and then spends
tens of milliseconds being JPEG-encoded and serialised, while a 32-byte
IMU sample leaves almost immediately. So by the time frame N arrives,
IMU samples stamped AFTER it have usually already been delivered.

The EKF loop consumes a monotonically increasing timeline - it predicts
forward on IMU and consumes a frame once time crosses that frame's
timestamp. Feeding it a frame stamped in its past means the frame is
either dropped or applied at the wrong pose. So events are held briefly
and emitted in timestamp order. The hold is the pipeline latency floor;
it is small next to the seconds of stereo baseline latency that follow.
"""
import heapq
import itertools
import socket
import time

from . import protocol
from .clock import SessionClock

KIND_FRAME = "frame"
KIND_IMU = "imu"
KIND_GPS = "gps"
KIND_INTRINSICS = "intrinsics"

# How long an event waits before it can be emitted, in SESSION seconds. Must
# exceed the spread in per-stream capture-to-receive latency, or late frames
# get dropped; every millisecond here is added to the live pose latency.
REORDER_HOLD_S = 0.25

# A frame whose timestamp is older than the already-emitted timeline cannot be
# placed. Counted and reported rather than silently discarded - a nonzero count
# means REORDER_HOLD_S is too small for this link.
class SessionStats:
    def __init__(self):
        self.frames = 0
        self.imu_samples = 0
        self.imu_batches = 0
        self.gps_fixes = 0
        self.bytes = 0
        self.late_dropped = 0
        self.frame_gaps = 0          # inter-frame intervals > FRAME_GAP_FACTOR x median
        self.first_frame_ms = None
        self.first_imu_ns = None
        self.started = time.monotonic()
        self._intervals = []
        # Rates are reported per second of CAPTURE, not per second of receiver
        # wall clock. When the consumer cannot keep up, TCP back-pressures the
        # phone and receiver time stretches; dividing by it then reports an IMU
        # rate of 19 Hz for a stream that genuinely carried 100 Hz, which is a
        # data-health alarm firing on a throughput problem.
        self.device_lo = None
        self.device_hi = None

    def note_event_time(self, t_s):
        self.device_lo = t_s if self.device_lo is None else min(self.device_lo, t_s)
        self.device_hi = t_s if self.device_hi is None else max(self.device_hi, t_s)

    @property
    def capture_span_s(self):
        if self.device_lo is None:
            return 0.0
        return self.device_hi - self.device_lo

    def note_frame_interval(self, dt_s):
        if dt_s > 0:
            self._intervals.append(dt_s)

    @property
    def median_frame_interval_s(self):
        if not self._intervals:
            return None
        s = sorted(self._intervals)
        return s[len(s) // 2]

    @property
    def measured_fps(self):
        m = self.median_frame_interval_s
        return (1.0 / m) if m else 0.0

    def summary(self):
        wall = max(time.monotonic() - self.started, 1e-9)
        cap = max(self.capture_span_s, 1e-9)
        lines = [
            "captured %.1f s of stream in %.1f s wall clock (%.2fx real time)"
            % (self.capture_span_s, wall, self.capture_span_s / wall),
            "  frames       %-8d (%.1f fps median, %.1f fps mean over capture)"
            % (self.frames, self.measured_fps, self.frames / cap),
            "  imu samples  %-8d (%.1f Hz over capture, %d batches)"
            % (self.imu_samples, self.imu_samples / cap, self.imu_batches),
            "  gps fixes    %-8d (%.1f Hz over capture)" % (self.gps_fixes, self.gps_fixes / cap),
            "  received     %.1f MB" % (self.bytes / 1e6),
        ]
        if self.capture_span_s > wall * 1.05:
            lines.append("  NOTE: the consumer ran slower than real time, so the phone was "
                         "back-pressured by TCP. Rates above are per second of capture.")
        if self.late_dropped:
            lines.append("  LATE-DROPPED %d events - raise REORDER_HOLD_S" % self.late_dropped)
        if self.frame_gaps:
            lines.append("  frame gaps   %d (link congestion or reconnects; "
                         "INTEGRATION.md section 4.2)" % self.frame_gaps)
        return "\n".join(lines)


FRAME_GAP_FACTOR = 2.5   # an interval this many times the median is a real gap,
                         # not jitter - the app drops frames on purpose when the
                         # link congests (bounded 10-frame queue, oldest evicted)


# --------------------------------------------------------------- sources --

def _serve_socket(conn, stats):
    """Yield (kind, payload, t_recv) until the peer hangs up."""
    conn.sendall(protocol.encode_handshake())
    while True:
        header = protocol.recv_exact(conn, 1)[0]
        t_recv = time.monotonic()
        if header == protocol.HEADER_FRAME:
            pkt = protocol.read_frame(conn)
            stats.bytes += 21 + len(pkt.jpeg)
            yield KIND_FRAME, pkt, t_recv
        elif header == protocol.HEADER_IMU:
            samples = protocol.read_imu(conn)
            stats.bytes += 3 + len(samples) * protocol.IMU_SAMPLE_BYTES
            yield KIND_IMU, samples, t_recv
        elif header == protocol.HEADER_GPS:
            pkt = protocol.read_gps(conn)
            stats.bytes += 1 + protocol.GPS_BYTES
            yield KIND_GPS, pkt, t_recv
        elif header == protocol.HEADER_INTRINSICS:
            intrinsics = protocol.read_intrinsics(conn)
            yield KIND_INTRINSICS, intrinsics, t_recv
        else:
            raise protocol.StreamClosed("unknown packet header 0x%02X" % header)


class SocketPacketSource:
    """Listens, accepts one phone, and yields its packets."""

    def __init__(self, host="0.0.0.0", port=5555, on_connect=None):
        self.host, self.port = host, port
        self.on_connect = on_connect
        self.peer = None

    def packets(self, stats):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(1)
        print("listening on %s:%d" % (self.host, self.port))
        try:
            conn, addr = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.peer = addr
            # Rates are measured from the moment the phone connects, not from
            # whenever the receiver was started - otherwise idle listening time
            # is averaged into the reported fps.
            stats.started = time.monotonic()
            print("phone connected: %s:%d" % addr)
            if self.on_connect:
                self.on_connect(addr)
            try:
                for event in _serve_socket(conn, stats):
                    yield event
            except protocol.StreamClosed as e:
                print("phone disconnected: %s" % e)
            except (ConnectionResetError, OSError) as e:
                print("phone dropped: %s" % e)
            finally:
                conn.close()
        finally:
            server.close()


# -------------------------------------------------------------- assembly --

class StreamSession:
    """Drives a packet source, aligns clocks, reorders, and fans out.

    Subscribers are duck-typed and may implement any of:

        on_session_start(clock, intrinsics_hint)
        on_frame(t_s, pkt)              t_s = session seconds
        on_imu(t_s, sample)             one sample at a time, in order
        on_gps(t_s, pkt, sigma_m)
        on_session_end(stats)
    """

    def __init__(self, source, subscribers, reorder_hold_s=REORDER_HOLD_S,
                 clock=None, verbose=True):
        self.source = source
        self.subscribers = list(subscribers)
        self.reorder_hold_s = reorder_hold_s
        self.clock = clock or SessionClock()
        self.stats = SessionStats()
        self.verbose = verbose

        self._heap = []                      # (t_s, seq, kind, payload)
        self._seq = itertools.count()
        self._emitted_upto = float("-inf")
        self._warmup_buffer = []             # raw packets held before t0 exists
        self._last_frame_t = None

    # -- fan-out helpers ---------------------------------------------------

    def _notify(self, method, *args):
        for sub in self.subscribers:
            fn = getattr(sub, method, None)
            if fn is not None:
                fn(*args)

    # -- main loop ---------------------------------------------------------

    def run(self):
        try:
            for kind, payload, t_recv in self.source.packets(self.stats):
                self._observe(kind, payload, t_recv)
                if kind == KIND_INTRINSICS:
                    # Metadata, not a timed event - _observe() already delivered
                    # it immediately, unconditionally. Never buffer or schedule
                    # it too, or on_intrinsics fires a second time once the
                    # warm-up buffer replays.
                    continue
                if not self.clock.ready:
                    self._warmup_buffer.append((kind, payload))
                    if self.clock.can_close():
                        self._close_warmup()
                    continue
                self._push(kind, payload)
                self._drain(until=self._latest_pushed_t - self.reorder_hold_s)
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            if not self.clock.ready:
                # The stream ended before the warm-up span was reached - a very
                # short session, or one delivered in a single burst. Close on
                # what arrived rather than discard it: the offset estimate is
                # then imprecise, which is worth saying out loud, but a short
                # capture is still worth reconstructing.
                if self.clock.force_close():
                    print("WARNING: stream ended after only %.2f s of capture; the "
                          "clock offset was estimated from %d frames / %d imu samples "
                          "and is correspondingly imprecise."
                          % (self.clock.warmup_elapsed_s(), self.stats.frames,
                             self.stats.imu_samples))
                    self._finish_warmup()
                else:
                    self._notify("on_session_end", self.stats)
                    raise SystemExit(
                        "stream carried no usable pairing of wall-clock and IMU packets "
                        "(%d frames, %d imu samples) - there is no timeline to build"
                        % (self.stats.frames, self.stats.imu_samples))
            self._drain(until=float("inf"))
            self._report_clock_check()
            self._notify("on_session_end", self.stats)

    def _observe(self, kind, payload, t_recv):
        if kind == KIND_FRAME:
            self.stats.frames += 1
            if self.stats.first_frame_ms is None:
                self.stats.first_frame_ms = payload.timestamp_ms
            self.clock.observe_frame(payload, t_recv)
        elif kind == KIND_IMU:
            self.stats.imu_batches += 1
            self.stats.imu_samples += len(payload)
            if self.stats.first_imu_ns is None and payload:
                self.stats.first_imu_ns = payload[0].t_ns
            self.clock.observe_imu(payload, t_recv)
        elif kind == KIND_GPS:
            self.stats.gps_fixes += 1
            self.clock.observe_gps(payload, t_recv)
        elif kind == KIND_INTRINSICS:
            self._notify("on_intrinsics", payload)

    def _close_warmup(self):
        self.clock.close_warmup()
        self._finish_warmup()

    def _finish_warmup(self):
        # Session zero is the earliest event actually seen, expressed on the
        # wall clock - so t=0 is a real instant in the capture rather than the
        # arbitrary moment the receiver happened to be scheduled.
        wall_candidates = [p.timestamp_ms / 1e3 for k, p in self._warmup_buffer
                           if k in (KIND_FRAME, KIND_GPS)]
        boot_candidates = [s.t_ns / 1e9 + self.clock.boot_to_wall_s
                           for k, p in self._warmup_buffer if k == KIND_IMU
                           for s in p]
        if wall_candidates or boot_candidates:
            self.clock.wall_t0_s = min(wall_candidates + boot_candidates)

        if self.verbose:
            print(self.clock.describe())
            raw_gap = None
            if self.stats.first_frame_ms and self.stats.first_imu_ns:
                raw_gap = self.stats.first_frame_ms / 1e3 - self.stats.first_imu_ns / 1e9
                print("raw domain gap before conversion: %.3e s "
                      "(expected; boot-ns vs epoch-ms)" % raw_gap)

        self._notify("on_session_start", self.clock, None)
        held, self._warmup_buffer = self._warmup_buffer, []
        for kind, payload in held:
            self._push(kind, payload)

    @property
    def _latest_pushed_t(self):
        return self._latest if hasattr(self, "_latest") else float("-inf")

    def _push(self, kind, payload):
        if kind == KIND_IMU:
            # Explode the batch: the EKF wants one sample at a time, and the
            # samples inside a batch each carry their own timestamp.
            for s in payload:
                self._schedule(self.clock.boot_ns_to_session_s(s.t_ns), kind, s)
        else:
            self._schedule(self.clock.wall_ms_to_session_s(payload.timestamp_ms),
                           kind, payload)

    def _schedule(self, t_s, kind, payload):
        self._latest = max(getattr(self, "_latest", float("-inf")), t_s)
        if t_s < self._emitted_upto:
            self.stats.late_dropped += 1
            return
        heapq.heappush(self._heap, (t_s, next(self._seq), kind, payload))

    def _drain(self, until):
        while self._heap and self._heap[0][0] <= until:
            t_s, _seq, kind, payload = heapq.heappop(self._heap)
            self._emitted_upto = t_s
            self._emit(t_s, kind, payload)

    def _emit(self, t_s, kind, payload):
        self.stats.note_event_time(t_s)
        if kind == KIND_IMU:
            self._notify("on_imu", t_s, payload)
        elif kind == KIND_FRAME:
            if self._last_frame_t is not None:
                dt = t_s - self._last_frame_t
                med = self.stats.median_frame_interval_s
                if med and dt > FRAME_GAP_FACTOR * med:
                    self.stats.frame_gaps += 1
                self.stats.note_frame_interval(dt)
            self._last_frame_t = t_s
            self._notify("on_frame", t_s, payload)
        elif kind == KIND_GPS:
            from .geodesy import gps_sigma_m
            self._notify("on_gps", t_s, payload, gps_sigma_m(payload.accuracy_m))

    def _report_clock_check(self):
        """INTEGRATION.md section 8: after conversion the domains must overlap."""
        if not (self.stats.first_frame_ms and self.stats.first_imu_ns):
            return
        f0 = self.clock.wall_ms_to_session_s(self.stats.first_frame_ms)
        i0 = self.clock.boot_ns_to_session_s(self.stats.first_imu_ns)
        ok, msg = self.clock.check_domains_overlap(
            (f0, self._emitted_upto), (i0, self._emitted_upto))
        print(("clock check OK: " if ok else "CLOCK CHECK FAILED: ") + msg)
