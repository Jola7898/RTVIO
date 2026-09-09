"""
Regression tests for the pose pipeline that replaced the EKF (see
CHANGELOG.md "Removed the EKF/IMU-dead-reckoning trajectory"):
GyroIntegrator, so3.level_and_align_attitude, and LiveReconstructor's
GPS-driven init + direct re-anchor + accuracy gate in live_pipeline.py.

Drives LiveReconstructor's on_imu/on_gps directly rather than through a real
socket (test_stream.py already covers StreamSession/SocketPacketSource
plumbing) - this file is about what those callbacks DO to self.pose_R/
self.pose_p, which is new logic no earlier test exercised.

    python tests/test_pose_pipeline.py
"""
import tempfile

import numpy as np

from rtvio.gyro_integrator import GyroIntegrator
from rtvio.so3 import level_and_align_attitude
from rtvio.stream import protocol
from rtvio.stream.geodesy import gps_sigma_m
from rtvio.live_pipeline import LiveReconstructor, build_argparser, MAX_GPS_REANCHOR_SIGMA_M

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-65s %s" % ("PASS" if ok else "FAIL", name, detail))


# ------------------------------------------------------------ GyroIntegrator --

def test_gyro_integrator_matches_analytic_rotation():
    """Constant angular velocity about Z for a known duration must integrate
    to that exact angle - the one thing this class exists to get right."""
    gi = GyroIntegrator()
    omega = 0.5  # rad/s about Z
    dt = 0.01
    gi.add_sample((0, 0, omega), 0.0)   # first sample only seeds prev_t
    t = 0.0
    for _ in range(100):
        t += dt
        gi.add_sample((0, 0, omega), t)
    delta = gi.consume()
    angle = np.arccos(np.clip((np.trace(delta) - 1) / 2, -1, 1))
    check("integrated angle matches omega * elapsed time",
          abs(angle - omega * t) < 1e-3, "%.5f vs %.5f rad" % (angle, omega * t))


def test_gyro_integrator_consume_resets():
    gi = GyroIntegrator()
    gi.add_sample((0, 0, 1.0), 0.0)
    gi.add_sample((0, 0, 1.0), 0.1)
    d1 = gi.consume()
    check("first consume is not identity", not np.allclose(d1, np.eye(3)))
    d2 = gi.consume()
    check("second consume with no new samples is identity", np.allclose(d2, np.eye(3)))


def test_gyro_integrator_skips_a_stale_gap():
    """A >0.5s gap (reconnect/stall) must not be integrated across - the
    same guard live_pipeline.py's old _ekf_step had."""
    gi = GyroIntegrator()
    gi.add_sample((0, 0, 10.0), 0.0)
    gi.add_sample((0, 0, 10.0), 2.0)   # 2s gap, would be 20 rad if integrated
    delta = gi.consume()
    check("a large gap is skipped, not integrated", np.allclose(delta, np.eye(3)))


# ------------------------------------------------------- level_and_align_attitude --

def test_leveling_recovers_tilted_gravity():
    """Accelerometer reading tilted 30deg off vertical must be leveled back
    to world +Z - the whole point of the function."""
    tilt = np.radians(30.0)
    accel_body = np.array([np.sin(tilt), 0.0, np.cos(tilt)]) * 9.81
    R = level_and_align_attitude(accel_body)
    world_z_in_body = R @ accel_body / np.linalg.norm(accel_body)
    check("leveled attitude maps measured gravity to world +Z",
          np.allclose(world_z_in_body, [0, 0, 1], atol=1e-6), world_z_in_body)


def test_course_heading_sets_yaw():
    """With a course supplied, the camera's image-up axis (+R[:,1]) must
    point along that course."""
    accel_body = np.array([0.0, 0.0, 9.81])
    course = np.radians(90.0)   # north
    R = level_and_align_attitude(accel_body, course_heading_rad=course)
    up_axis = R[:, 1]
    heading = np.arctan2(up_axis[1], up_axis[0])
    check("image-up axis aligned to the supplied course",
          abs(heading - course) < 1e-6, "%.4f vs %.4f rad" % (heading, course))


# ------------------------------------------------------------- LiveReconstructor --

REF_LAT, REF_LON, REF_ALT = 37.7749, -122.4194, 30.0
M_PER_DEG_LAT = 111_320.0


def _gps(t_s, dx_m, dy_m, accuracy_m, alt_m=REF_ALT):
    """A GpsPacket dx_m east / dy_m north of the (REF_LAT, REF_LON) origin -
    small-angle flat-Earth approximation, good enough for a metre-scale test."""
    lat = REF_LAT + dy_m / M_PER_DEG_LAT
    lon = REF_LON + dx_m / (M_PER_DEG_LAT * np.cos(np.radians(REF_LAT)))
    return protocol.GpsPacket(int(t_s * 1000), lat, lon, alt_m, accuracy_m)


def _recon():
    args = build_argparser().parse_args([])
    intrinsics = {"fx": 1000.0, "fy": 1000.0, "cx": 640.0, "cy": 360.0,
                  "width": 1280, "height": 720}
    d = tempfile.mkdtemp()
    r = LiveReconstructor(d, intrinsics, args)
    r.on_session_start(clock=None, _hint=None)
    return r


def test_init_from_gps_course_seeds_pose_and_yaw():
    r = _recon()
    for i in range(3):
        r.on_imu(0.01 * i, protocol.ImuSample(0, (0.0, 0.0, 9.81), (0.0, 0.0, 0.0)))
    check("not initialised before any GPS", r.initialized is False)

    # Six fixes, 5m apart, due east - well past COURSE_MIN_FIXES/DISTANCE_M.
    for i in range(6):
        pkt = _gps(1.0 + i, dx_m=5.0 * i, dy_m=0.0, accuracy_m=3.0)
        r.on_gps(1.0 + i, pkt, gps_sigma_m(pkt.accuracy_m))

    check("initialises once heading is observable", r.initialized is True)
    check("initial course is ~east (0 deg)", abs(r.init_course_deg) < 2.0,
          "%.2f deg" % r.init_course_deg)
    check("pose seeded near the last buffered fix (25m east)",
          np.allclose(r.pose_p, [25.0, 0.0, 0.0], atol=0.5), r.pose_p)
    check("gps_used counts every accepted fix", r.gps_used == 6, r.gps_used)


def test_post_init_gps_snaps_pose_directly():
    r = _recon()
    for i in range(3):
        r.on_imu(0.01 * i, protocol.ImuSample(0, (0.0, 0.0, 9.81), (0.0, 0.0, 0.0)))
    for i in range(6):
        pkt = _gps(1.0 + i, dx_m=5.0 * i, dy_m=0.0, accuracy_m=3.0)
        r.on_gps(1.0 + i, pkt, gps_sigma_m(pkt.accuracy_m))
    assert r.initialized

    pkt = _gps(10.0, dx_m=0.0, dy_m=40.0, accuracy_m=2.0)   # far off the prior track
    r.on_gps(10.0, pkt, gps_sigma_m(pkt.accuracy_m))
    check("a good post-init fix snaps pose_p exactly to it (no smoothing)",
          np.allclose(r.pose_p, [0.0, 40.0, 0.0], atol=0.5), r.pose_p)
    check("gps_used incremented", r.gps_used == 7)
    check("gps_seen incremented", r.gps_seen == 7)  # 6 replayed at init + this one


def test_post_init_low_accuracy_gps_is_rejected_not_applied():
    r = _recon()
    for i in range(3):
        r.on_imu(0.01 * i, protocol.ImuSample(0, (0.0, 0.0, 9.81), (0.0, 0.0, 0.0)))
    for i in range(6):
        pkt = _gps(1.0 + i, dx_m=5.0 * i, dy_m=0.0, accuracy_m=3.0)
        r.on_gps(1.0 + i, pkt, gps_sigma_m(pkt.accuracy_m))
    assert r.initialized
    p_before = r.pose_p.copy()

    # accuracy_m past MAX_GPS_REANCHOR_SIGMA_M (clamped by gps_sigma_m to 50,
    # still well over the 15m reanchor gate) - must not move the pose.
    bad_accuracy = MAX_GPS_REANCHOR_SIGMA_M + 10.0
    pkt = _gps(10.0, dx_m=500.0, dy_m=500.0, accuracy_m=bad_accuracy)
    r.on_gps(10.0, pkt, gps_sigma_m(pkt.accuracy_m))
    check("a low-accuracy fix does not move pose_p",
          np.allclose(r.pose_p, p_before), r.pose_p)
    check("gps_used NOT incremented", r.gps_used == 6, r.gps_used)
    check("gps_seen incremented", r.gps_seen == 7)  # 6 replayed at init + this one
    check("gps_rejected_low_accuracy incremented", r.gps_rejected_low_accuracy == 1)


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
