"""
Acceptance tests for the live-ingest path (INTEGRATION.md section 8).

These are the checks worth having before you need them - each one
corresponds to a documented way the integration fails SILENTLY, producing
a plausible reconstruction that is wrong rather than an exception.

    python tests/test_stream.py

Imports `rtvio` as an installed package (`pip install -e .` from the repo
root) rather than patching sys.path - see pyproject.toml.
"""
import math
import os
import random
import sys

import numpy as np

from rtvio.stream import protocol
from rtvio.stream.clock import SessionClock
from rtvio.stream.geodesy import (latlon_to_enu, enu_to_latlon, gps_sigma_m,
                            AltitudeSanity, DEFAULT_GPS_SIGMA_M,
                            MIN_GPS_SIGMA_M, MAX_GPS_SIGMA_M)
from rtvio import georeference

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


# ---------------------------------------------------------------- protocol --

def test_wire_roundtrip():
    """Every packet type must survive encode -> decode unchanged. The encoder
    is what tools/replay_dataset_as_phone.py uses to impersonate the handset,
    so if it drifts from the decoder the test double stops being a test."""
    class FakeSock:
        def __init__(self, blob):
            self.buf, self.pos = blob, 0

        def recv(self, n):
            chunk = self.buf[self.pos:self.pos + min(n, 3)]   # deliberately short
            self.pos += len(chunk)
            return chunk

    frame = protocol.FramePacket(1793800000123, 960, 540, b"\xff\xd8" + b"j" * 5000)
    imu = [protocol.ImuSample(493122000000000 + i * 10_000_000,
                              (0.1 * i, 9.81, -0.2), (0.01, -0.02, 0.003))
           for i in range(7)]
    gps = protocol.GpsPacket(1793800000456, 12.9716, 77.5946, 921.5, 4.25)

    blob = (protocol.encode_frame(frame) + protocol.encode_imu_batch(imu)
            + protocol.encode_gps(gps))
    s = FakeSock(blob)

    k, f = protocol.read_packet(s)
    ok_f = k == protocol.HEADER_FRAME and f == frame
    k, i = protocol.read_packet(s)
    ok_i = k == protocol.HEADER_IMU and len(i) == len(imu) and all(
        a.t_ns == b.t_ns and np.allclose(a.accel, b.accel, atol=1e-6)
        and np.allclose(a.gyro, b.gyro, atol=1e-6) for a, b in zip(i, imu))
    k, g = protocol.read_packet(s)
    ok_g = (k == protocol.HEADER_GPS and g.timestamp_ms == gps.timestamp_ms
            and abs(g.lat_deg - gps.lat_deg) < 1e-12
            and abs(g.altitude_m - gps.altitude_m) < 1e-3)

    check("wire round-trip, all three packet types", ok_f and ok_i and ok_g,
          "recv() returning short reads is handled")


def test_short_read_handling():
    """sock.recv(n) may return fewer than n bytes. The FakeSock above caps
    every read at 3 bytes, so a decoder that called recv once per field would
    already have failed. This asserts the ceiling guards too."""
    class Desync:
        def __init__(self, blob):
            self.buf, self.pos = blob, 0

        def recv(self, n):
            chunk = self.buf[self.pos:self.pos + n]
            self.pos += len(chunk)
            return chunk

    import struct
    bad = bytes([protocol.HEADER_FRAME]) + struct.pack(
        protocol.FRAME_HEADER_FMT, 0, 960, 540, 999_999_999)
    try:
        protocol.read_packet(Desync(bad))
        ok = False
    except protocol.StreamClosed:
        ok = True
    check("desynchronised stream is rejected, not allocated", ok,
          "implausible jpeg_size raises instead of reserving 1 GB")


# ------------------------------------------------------------------- clock --

def test_clock_domains():
    """IMU is monotonic-since-boot ns; frames and GPS are epoch ms. After
    conversion both must land on the same session timeline. A ~1e12 result
    means section 4.1 was skipped."""
    WALL0, BOOT0 = 1.7938e9, 493122.0
    FRAME_LAT, IMU_LAT = 0.050, 0.004
    t = [0.0]
    c = SessionClock(warmup_s=0.0, now=lambda: t[0])
    rng = random.Random(11)
    for i in range(60):
        ts = i * 0.03
        t[0] = WALL0 + ts + FRAME_LAT + rng.expovariate(40)
        c.observe_frame(protocol.FramePacket(int((WALL0 + ts) * 1e3), 960, 540, b""))
        t[0] = WALL0 + ts + IMU_LAT + rng.expovariate(300)
        c.observe_imu([protocol.ImuSample(int((BOOT0 + ts) * 1e9), (0, 0, 0), (0, 0, 0))])
    c.close_warmup()

    truth = WALL0 - BOOT0
    err_ms = abs(c.boot_to_wall_s - truth) * 1e3
    # The minimum filter removes jitter but cannot remove the DIFFERENCE in
    # per-stream capture-to-send latency; that residual is the documented
    # limitation and the reason this should become an online EKF parameter.
    expected_ms = (FRAME_LAT - IMU_LAT) * 1e3
    check("clock offset estimate is jitter-free", abs(err_ms - expected_ms) < 3.0,
          "error %.1f ms, explained by the %.0f ms latency difference" % (err_ms, expected_ms))

    probe = 7.5
    f_s = c.wall_ms_to_session_s(int((WALL0 + probe) * 1e3))
    i_s = c.boot_ns_to_session_s(int((BOOT0 + probe) * 1e9))
    check("both domains land on one timeline", abs(f_s - i_s) < 0.1,
          "same instant -> %.4fs vs %.4fs" % (f_s, i_s))

    ok, _ = c.check_domains_overlap((0.0, 20.0), (1.7e12, 1.7e12 + 20))
    check("skipped conversion is caught, not tolerated", not ok,
          "a 1e12 gap is reported as an error")


# ----------------------------------------------------------------- geodesy --

def test_enu_roundtrip():
    """enu -> latlon -> enu must agree to well under a millimetre, and the
    conversion must be the exact inverse of the one georeference.py already
    uses - two mutually inconsistent conversions put the model in the wrong
    place with no other symptom."""
    ref = (12.9716, 77.5946, 900.0)
    worst = 0.0
    for e, n, u in [(0, 0, 0), (150.0, -230.0, 42.0), (-1000.0, 800.0, -75.0),
                    (12.345, 67.89, 1.0)]:
        lat, lon, alt = enu_to_latlon(e, n, u, *ref)
        e2, n2, u2 = latlon_to_enu(lat, lon, alt, *ref)
        worst = max(worst, abs(e2 - e), abs(n2 - n), abs(u2 - u))
    check("ENU round-trip closes", worst < 1e-6, "worst error %.3e m" % worst)

    # Same origin, same numbers as the module the pipeline georeferences with.
    lat, lon, alt = enu_to_latlon(123.0, -45.0, 6.0, *ref)
    glat, glon, galt = georeference.enu_to_latlon(123.0, -45.0, 6.0, *ref)
    agree = max(abs(lat - glat), abs(lon - glon), abs(alt - galt))
    check("agrees with georeference.enu_to_latlon exactly", agree < 1e-12,
          "max disagreement %.3e deg" % agree)


def test_gps_sigma():
    check("unknown GPS accuracy falls back, not to zero",
          gps_sigma_m(-1.0) == DEFAULT_GPS_SIGMA_M,
          "-1 -> %.1f m" % gps_sigma_m(-1.0))
    check("GPS sigma is clamped both ways",
          gps_sigma_m(0.01) == MIN_GPS_SIGMA_M and gps_sigma_m(9999.0) == MAX_GPS_SIGMA_M,
          "an over-optimistic fix cannot dominate the filter")
    check("a good fix passes through", abs(gps_sigma_m(3.5) - 3.5) < 1e-9)

    a = AltitudeSanity()
    for _ in range(10):
        a.observe(921.0)
    check("constant altitude is flagged", len(a.warnings()) == 1,
          a.warnings()[0][:58] if a.warnings() else "")


# ------------------------------------------------- dataset <-> live parity --

def test_dataset_gps_consistency():
    """The synthetic dataset's lat/lon and its enu_xyz_noisy must be two views
    of one position. The replay tool sends lat/lon and the live path recomputes
    ENU, so if these disagree the comparison against the batch pipeline is
    meaningless."""
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(root, "flight_config.json")
    gps_path = os.path.join(root, "gps_data.json")
    if not os.path.exists(cfg_path) or not os.path.exists(gps_path):
        print("  (skipped test_dataset_gps_consistency: no synthetic dataset "
              "- flight_config.json/gps_data.json were removed)")
        return
    cfg = json.load(open(cfg_path))
    gps = json.load(open(gps_path))
    ref = (cfg["ref_lat_deg"], cfg["ref_lon_deg"], cfg["ref_alt_m"])

    worst = 0.0
    for g in gps[:50]:
        e, n, u = latlon_to_enu(g["latitude_deg"], g["longitude_deg"],
                                ref[2] + g["enu_xyz_noisy"][2], *ref)
        want = g["enu_xyz_noisy"]
        worst = max(worst, abs(e - want[0]), abs(n - want[1]), abs(u - want[2]))
    check("recomputed ENU matches the dataset's own", worst < 1e-6,
          "worst error %.3e m over 50 fixes" % worst)


# --------------------------------------------------------- rolling window --

def test_rolling_window_bounded():
    """The live path must hold a bounded window, not the session. This is the
    difference between a 250 MB working set and a 9 GB OOM at 1080p."""
    from rtvio.live_pipeline import RollingWindow
    from rtvio import dense_stereo

    w = RollingWindow(keyframe_stride=8, search_ahead=dense_stereo.SEARCH_AHEAD_FRAMES)
    next_kf = 0
    for i in range(5000):
        w.append({"idx": i, "t": i / 30.0})
        if i - next_kf > dense_stereo.SEARCH_AHEAD_FRAMES:
            next_kf += 8
            w.evict_before(next_kf)
    check("rolling window stays bounded over a long session",
          w.peak_len <= dense_stereo.SEARCH_AHEAD_FRAMES + 16,
          "peak %d frames after 5000 (batch would hold 5000)" % w.peak_len)


# -------------------------------------------------------- reorder buffer --

class _Collector:
    def __init__(self):
        self.seen = []

    def on_session_start(self, clock, hint):
        pass

    def on_frame(self, t_s, pkt):
        self.seen.append(("frame", t_s))

    def on_imu(self, t_s, s):
        self.seen.append(("imu", t_s))

    def on_gps(self, t_s, pkt, sigma):
        self.seen.append(("gps", t_s))

    def on_session_end(self, stats):
        pass


class _ScriptedSource:
    """Emits a fixed packet script with controllable per-stream latency."""

    def __init__(self, n=90, frame_lat=0.12, imu_lat=0.002, jitter=0.0, seed=3):
        self.n, self.frame_lat, self.imu_lat = n, frame_lat, imu_lat
        self.jitter, self.seed = jitter, seed

    def packets(self, stats):
        WALL0, BOOT0 = 1.7938e9, 493122.0
        rng = random.Random(self.seed)
        events = []
        for i in range(self.n):
            t = i * 0.033
            lat = self.frame_lat + (rng.expovariate(1.0 / self.jitter) if self.jitter else 0.0)
            events.append((t + lat, "f",
                           protocol.FramePacket(int((WALL0 + t) * 1e3), 96, 54, b"x")))
        for i in range(self.n * 3):
            t = i * 0.011
            events.append((t + self.imu_lat, "i",
                           [protocol.ImuSample(int((BOOT0 + t) * 1e9),
                                               (0.0, 0.0, 9.81), (0.0, 0.0, 0.0))]))
        events.sort(key=lambda e: e[0])       # ARRIVAL order, not capture order
        for arrive, kind, payload in events:
            yield ("frame" if kind == "f" else "imu"), payload, arrive


def test_reorder_emits_in_timestamp_order():
    """A frame is stamped at capture then spends tens of ms being JPEG-encoded
    and serialised, while a 32-byte IMU sample leaves immediately. So frames
    arrive AFTER IMU samples stamped later than them. The EKF consumes a
    monotonically increasing timeline, so anything emitted out of order is
    either dropped or applied at the wrong pose - silently."""
    from rtvio.stream.source import StreamSession
    from rtvio.stream.clock import SessionClock

    c = _Collector()
    clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)
    sess = StreamSession(_ScriptedSource(), [c], reorder_hold_s=0.25,
                         clock=clock, verbose=False)
    sess.run()

    times = [t for _k, t in c.seen]
    monotonic = all(b >= a for a, b in zip(times, times[1:]))
    check("reorder buffer emits in timestamp order", monotonic,
          "%d events, frames arriving %.0f ms behind IMU" % (len(times), 118))
    check("nothing was dropped as late", sess.stats.late_dropped == 0,
          "late_dropped=%d with a %.2fs hold" % (sess.stats.late_dropped, 0.25))
    kinds = set(k for k, _t in c.seen)
    check("both streams reached the consumer", kinds == {"frame", "imu"},
          "saw %s" % sorted(kinds))


def test_reorder_hold_too_small_is_reported():
    """It is JITTER, not constant latency, that the reorder buffer exists for.

    A constant per-stream latency difference is absorbed by the clock's
    minimum filter and re-labelled as a clock offset - wrong for accuracy
    (see clock.py) but harmless for ordering, because everything shifts
    together. Variable latency cannot be absorbed that way: a frame that
    happens to take 300 ms when the floor is 40 ms lands behind IMU already
    emitted. Those must be counted, never silently discarded, because a
    nonzero count is the only signal that REORDER_HOLD_S is too small for
    this link.
    """
    from rtvio.stream.source import StreamSession
    from rtvio.stream.clock import SessionClock

    c = _Collector()
    clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)
    sess = StreamSession(_ScriptedSource(frame_lat=0.04, jitter=0.25), [c],
                         reorder_hold_s=0.01, clock=clock, verbose=False)
    sess.run()
    check("an undersized reorder hold is counted, not hidden",
          sess.stats.late_dropped > 0,
          "%d jittered events reported as late" % sess.stats.late_dropped)

    # The same stream with an adequate hold must lose nothing.
    c2 = _Collector()
    clock2 = SessionClock(warmup_s=0.3, now=lambda: 0.0)
    sess2 = StreamSession(_ScriptedSource(frame_lat=0.04, jitter=0.25), [c2],
                          reorder_hold_s=1.5, clock=clock2, verbose=False)
    sess2.run()
    times = [t for _k, t in c2.seen]
    check("a large enough hold absorbs the same jitter",
          sess2.stats.late_dropped == 0 and all(b >= a for a, b in zip(times, times[1:])),
          "late_dropped=%d, order monotonic" % sess2.stats.late_dropped)


# -------------------------------------------------------------- intrinsics --

class _IntrinsicsThenStream:
    """One intrinsics packet up front, then a normal frame/imu script.

    Mirrors what the phone actually does: offerIntrinsics() fires once, right
    after the socket connects, before the first frame or IMU sample.
    """

    def __init__(self, inner):
        self.inner = inner

    def packets(self, stats):
        yield ("intrinsics",
               protocol.IntrinsicsPacket(1000.0, 1000.0, 480.0, 270.0,
                                         0.0, 0.0, 0.0, 0.0, 0.0, "test"),
               0.0)
        yield from self.inner.packets(stats)


def test_intrinsics_delivered_exactly_once():
    """on_intrinsics has no timestamp, so it bypasses the clock/reorder
    machinery entirely and fires as soon as the packet is seen. That path
    used to run twice for the same packet - once immediately, once again
    when the warm-up buffer replayed - which is harmless for the K matrix
    (same value both times) but is exactly the kind of double-fire that
    silently corrupts a counter or a once-only side effect."""
    from rtvio.stream.source import StreamSession
    from rtvio.stream.clock import SessionClock

    class Collector(_Collector):
        def __init__(self):
            super().__init__()
            self.intrinsics_calls = 0

        def on_intrinsics(self, pkt):
            self.intrinsics_calls += 1

    c = Collector()
    clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)
    sess = StreamSession(_IntrinsicsThenStream(_ScriptedSource()), [c],
                         reorder_hold_s=0.25, clock=clock, verbose=False)
    sess.run()
    check("on_intrinsics fires exactly once", c.intrinsics_calls == 1,
          "fired %d times" % c.intrinsics_calls)


def main():
    test_wire_roundtrip()
    test_short_read_handling()
    test_clock_domains()
    test_enu_roundtrip()
    test_gps_sigma()
    test_dataset_gps_consistency()
    test_rolling_window_bounded()
    test_reorder_emits_in_timestamp_order()
    test_reorder_hold_too_small_is_reported()
    test_intrinsics_delivered_exactly_once()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
