"""
Integration test for Tracker._relative_reinit - the periodic two-view
relative-pose reconstruction path added to fix indoor sessions where the
incremental (absolute-pose-based) path never promotes anything, because
without GPS the absolute pose drifts too far for its own baseline/depth/
reprojection gates to trust (measured: 24m of drift over a 49s session
that was physically a few metres - see tracking.py's MIN_BASELINE_M
comment and CHANGELOG.md).

This drives _relative_reinit directly (bypassing real LK/ORB, which need
pixel-correlated real images) with synthetic, known 2D correspondences and
poses, so the whole snapshot/track-ID/Essential-matrix/promotion path is
exercised end to end against known ground truth - not just the isolated
math helper (see test_geometry.py's test_recover_relative_pose_roundtrip).

    python tests/test_relative_reinit.py
"""
import numpy as np
import cv2

from rtvio.tracking import Tracker, build_projection_matrix

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


K = np.array([[800.0, 0, 480.0], [0, 800.0, 270.0], [0, 0, 1.0]])


def _pose(seed):
    rng = np.random.default_rng(seed)
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    angle = rng.uniform(-0.2, 0.2)
    skew = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    R = np.eye(3) + np.sin(angle) * skew + (1 - np.cos(angle)) * (skew @ skew)
    p = rng.normal(scale=0.3, size=3)
    return R, p


def test_relative_reinit_recovers_points_across_a_real_baseline():
    rng = np.random.default_rng(99)
    R_ref, p_ref = np.eye(3), np.zeros(3)
    R_cur, p_cur = _pose(1)
    p_cur = p_ref + np.array([0.4, 0.1, 0.0])  # ~0.4m real handheld-scale translation

    # A room-scale static scene, 1-3m in front of the reference camera.
    pts3d = p_ref + rng.normal(scale=0.8, size=(80, 3)) * [1, 1, 0.3] + R_ref[:, 2] * -2.0

    P_ref = build_projection_matrix(K, R_ref, p_ref)
    P_cur = build_projection_matrix(K, R_cur, p_cur)

    def project(P, X):
        h = P @ np.append(X, 1.0)
        return (h[:2] / h[2]).astype(np.float32)

    px_ref = np.array([project(P_ref, X) for X in pts3d], dtype=np.float32)
    px_cur = np.array([project(P_cur, X) for X in pts3d], dtype=np.float32)

    tracker = Tracker(K, run_bundle_adjustment=False)
    tracker.MIN_ESSENTIAL_MATCHES = 20
    tracker.MIN_ESSENTIAL_INLIERS = 15
    tracker.REINIT_MIN_WINDOW_S = 0.2
    tracker.REINIT_MAX_WINDOW_S = 2.0
    tracker.MIN_DEPTH_M, tracker.MAX_DEPTH_M = 0.3, 8.0    # room-scale, like --indoor
    tracker.active_px = px_ref.copy()
    tracker.track_id = list(range(len(pts3d)))
    tracker.next_track_id = len(pts3d)
    tracker.map_idx = [-1] * len(pts3d)

    blank = np.full((540, 960, 3), 128, dtype=np.uint8)

    # First call just seeds the reference snapshot (no motion yet to compare against).
    n0 = tracker._relative_reinit(R_ref, p_ref, blank, t_s=0.0)
    check("first call seeds the snapshot, promotes nothing yet", n0 == 0)

    # Advance past REINIT_MIN_WINDOW_S (real elapsed time, not frame count) and
    # present the "moved" correspondences.
    tracker.active_px = px_cur.copy()
    n1 = tracker._relative_reinit(R_cur, p_cur, blank, t_s=0.5)
    check("a real baseline promotes new map points", n1 > 0, "promoted %d" % n1)
    check("promoted points are a healthy fraction of correspondences",
          n1 >= 0.3 * len(pts3d), "%d / %d" % (n1, len(pts3d)))

    recovered = np.array(tracker.points)
    if len(recovered):
        # Match each recovered point to its nearest ground-truth point (order isn't
        # preserved 1:1 because some tracks may fail individual gates).
        errs = []
        for X in recovered:
            errs.append(np.min(np.linalg.norm(pts3d - X, axis=1)))
        check("recovered points land close to true 3D positions",
              np.median(errs) < 0.05, "median error %.4f m" % np.median(errs))


def test_relative_reinit_ignores_too_little_motion():
    """MIN_REINIT_SCALE_M must reject a window where the EKF itself barely
    moved - otherwise a near-zero scale estimate would blow up into
    arbitrarily bad 3D points (dividing information by noise)."""
    R_ref, p_ref = np.eye(3), np.zeros(3)
    rng = np.random.default_rng(7)
    pts3d = p_ref + rng.normal(scale=0.8, size=(40, 3)) * [1, 1, 0.3] + R_ref[:, 2] * -2.0
    P_ref = build_projection_matrix(K, R_ref, p_ref)

    def project(P, X):
        h = P @ np.append(X, 1.0)
        return (h[:2] / h[2]).astype(np.float32)

    px_ref = np.array([project(P_ref, X) for X in pts3d], dtype=np.float32)

    tracker = Tracker(K, run_bundle_adjustment=False)
    tracker.REINIT_MIN_WINDOW_S = 0.2
    tracker.REINIT_MAX_WINDOW_S = 2.0
    tracker.active_px = px_ref.copy()
    tracker.track_id = list(range(len(pts3d)))
    tracker.next_track_id = len(pts3d)
    tracker.map_idx = [-1] * len(pts3d)
    blank = np.full((540, 960, 3), 128, dtype=np.uint8)

    tracker._relative_reinit(R_ref, p_ref, blank, t_s=0.0)
    # Camera "moved" by 1mm - well under MIN_REINIT_SCALE_M.
    n = tracker._relative_reinit(R_ref, p_ref + np.array([0.001, 0, 0]), blank, t_s=0.5)
    check("a near-zero-motion window promotes nothing", n == 0, "promoted %d" % n)


def test_relative_reinit_ignores_a_too_stale_window():
    """REINIT_MAX_WINDOW_S: if too much REAL time elapsed (congestion, not
    just a slow processed-frame count), the EKF's position delta is no
    longer trustworthy for scale - the window must be discarded, not used."""
    R_ref, p_ref = np.eye(3), np.zeros(3)
    R_cur, p_cur = _pose(2)
    p_cur = p_ref + np.array([0.4, 0.1, 0.0])
    rng = np.random.default_rng(5)
    pts3d = p_ref + rng.normal(scale=0.8, size=(40, 3)) * [1, 1, 0.3] + R_ref[:, 2] * -2.0
    P_ref = build_projection_matrix(K, R_ref, p_ref)
    P_cur = build_projection_matrix(K, R_cur, p_cur)

    def project(P, X):
        h = P @ np.append(X, 1.0)
        return (h[:2] / h[2]).astype(np.float32)

    px_ref = np.array([project(P_ref, X) for X in pts3d], dtype=np.float32)
    px_cur = np.array([project(P_cur, X) for X in pts3d], dtype=np.float32)

    tracker = Tracker(K, run_bundle_adjustment=False)
    tracker.MIN_ESSENTIAL_MATCHES = 20
    tracker.MIN_ESSENTIAL_INLIERS = 15
    tracker.REINIT_MIN_WINDOW_S = 0.2
    tracker.REINIT_MAX_WINDOW_S = 2.0
    tracker.MIN_DEPTH_M, tracker.MAX_DEPTH_M = 0.3, 8.0
    tracker.active_px = px_ref.copy()
    tracker.track_id = list(range(len(pts3d)))
    tracker.next_track_id = len(pts3d)
    tracker.map_idx = [-1] * len(pts3d)
    blank = np.full((540, 960, 3), 128, dtype=np.uint8)

    tracker._relative_reinit(R_ref, p_ref, blank, t_s=0.0)
    tracker.active_px = px_cur.copy()
    # 5 seconds of real elapsed time - well past REINIT_MAX_WINDOW_S=2.0,
    # simulating a congested stretch even though the "motion" is identical
    # to the healthy case above.
    n = tracker._relative_reinit(R_cur, p_cur, blank, t_s=5.0)
    check("a too-stale window promotes nothing rather than trusting bad scale",
          n == 0, "promoted %d" % n)


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
