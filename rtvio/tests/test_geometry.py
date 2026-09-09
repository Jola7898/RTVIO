"""
Regression tests for the camera-geometry conventions.

Every serious bug this pipeline has had so far has been a convention
mismatch rather than a wrong formula: a pose expressed in Blender camera
axes (forward = -R[:,2]) fed into code that assumed OpenCV axes
(forward = +R[:,2]), or a mesh exported in Wavefront axes and compared
against a trajectory in ENU. Neither one crashes, and neither one is
visible in a stack trace - they just quietly make the output wrong, which
is exactly the kind of thing a test is for.

Run with: python tests/test_geometry.py   (plain asserts, no pytest needed)

Imports `rtvio` as an installed package (`pip install -e .` from the repo
root) rather than patching sys.path - see pyproject.toml.
"""
import numpy as np
import cv2

from rtvio import dense_stereo as ds
from rtvio.tracking import (CV_FROM_BODY, build_projection_matrix,
                            triangulate_point, recover_relative_pose)

K = np.array([[640.0, 0, 480.0], [0, 640.0, 270.0], [0, 0, 1.0]])


def _sample_pose(seed=0):
    """A pose in this repo's convention: body-to-world R with the camera
    looking along -R[:,2], roughly nadir from 80 m like the real flight."""
    rng = np.random.default_rng(seed)
    yaw = rng.uniform(-np.pi, np.pi)
    tilt = np.radians(15.0)
    Rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1.0]])
    Rx = np.array([[1, 0, 0], [0, np.cos(tilt), -np.sin(tilt)], [0, np.sin(tilt), np.cos(tilt)]])
    R = Rz @ Rx
    p = np.array([rng.uniform(-50, 50), rng.uniform(-50, 50), 80.0])
    return R, p


def test_cv_from_body_is_a_rotation():
    assert np.allclose(CV_FROM_BODY @ CV_FROM_BODY, np.eye(3)), "S must be its own inverse"
    assert np.isclose(np.linalg.det(CV_FROM_BODY), 1.0), "S must be a rotation, not a reflection"


def test_forward_axis_is_minus_z():
    """A point placed straight down the camera's -R[:,2] axis must land at
    the principal point with positive depth. This is the single assertion
    that pins which of the two conventions the repo uses."""
    R, p = _sample_pose(1)
    X = (p - 60.0 * R[:, 2])[None, :]
    pix, depth = ds._world_to_pixels(X, R, p, K)
    assert np.allclose(pix[0], [K[0, 2], K[1, 2]], atol=1e-6), pix
    assert np.isclose(depth[0], 60.0), depth


def test_pixel_world_roundtrip():
    R, p = _sample_pose(2)
    rng = np.random.default_rng(3)
    u = rng.uniform(0, 960, 500)
    v = rng.uniform(0, 540, 500)
    d = rng.uniform(20, 200, 500)
    Xw = ds._pixel_to_world_at_depth(u, v, d, R, p, K)
    pix, depth = ds._world_to_pixels(Xw, R, p, K)
    assert np.allclose(pix[:, 0], u, atol=1e-6)
    assert np.allclose(pix[:, 1], v, atol=1e-6)
    assert np.allclose(depth, d, atol=1e-9)


def test_projection_matrix_matches_world_to_pixels():
    """tracking.py's 3x4 projection matrix and dense_stereo's explicit
    projection are two independent implementations of the same mapping;
    they used to disagree by a 180-degree flip about the camera X axis."""
    R, p = _sample_pose(4)
    rng = np.random.default_rng(5)
    Xw = np.column_stack([rng.uniform(-100, 100, 300), rng.uniform(-100, 100, 300),
                          rng.uniform(-20, 20, 300)])
    P = build_projection_matrix(K, R, p)
    hom = np.column_stack([Xw, np.ones(len(Xw))]) @ P.T
    in_front = hom[:, 2] > 1e-6
    proj = hom[in_front, :2] / hom[in_front, 2:3]

    pix, depth = ds._world_to_pixels(Xw, R, p, K)
    assert np.array_equal(in_front, depth > 1e-6), "the two disagree about which points are in front"
    assert np.allclose(proj, pix[in_front], atol=1e-6)


def test_triangulation_recovers_known_points():
    R1, p1 = _sample_pose(6)
    R2, p2 = R1.copy(), p1 + np.array([8.0, 1.0, 0.0])
    rng = np.random.default_rng(7)
    Xw = np.column_stack([rng.uniform(-40, 40, 60), rng.uniform(-40, 40, 60),
                          rng.uniform(-5, 15, 60)])
    pix1, d1 = ds._world_to_pixels(Xw, R1, p1, K)
    pix2, d2 = ds._world_to_pixels(Xw, R2, p2, K)
    keep = (d1 > 1) & (d2 > 1)
    P1, P2 = build_projection_matrix(K, R1, p1), build_projection_matrix(K, R2, p2)
    for i in np.where(keep)[0]:
        X = triangulate_point(P1, P2, pix1[i], pix2[i])
        assert np.allclose(X, Xw[i], atol=1e-6), (X, Xw[i])


def test_solvepnp_roundtrip():
    """The exact conversion tracking.py applies to solvePnPRansac's output
    before handing it to the EKF as an attitude measurement. Getting this
    wrong leaves the measurement 180 degrees out, which the EKF's
    innovation gate silently rejects - no error, just no vision."""
    R, p = _sample_pose(8)
    rng = np.random.default_rng(9)
    Xw = np.column_stack([rng.uniform(-40, 40, 200), rng.uniform(-40, 40, 200),
                          rng.uniform(-5, 15, 200)])
    pix, depth = ds._world_to_pixels(Xw, R, p, K)
    keep = (depth > 1) & (pix[:, 0] > 0) & (pix[:, 0] < 960) & (pix[:, 1] > 0) & (pix[:, 1] < 540)
    assert keep.sum() >= 20, "test setup produced too few visible points"

    ok, rvec, tvec, inliers = cv2.solvePnPRansac(
        Xw[keep].astype(np.float32), pix[keep].astype(np.float32), K, None,
        reprojectionError=1.0, confidence=0.999)
    assert ok and inliers is not None and len(inliers) >= 20

    R_cam, _ = cv2.Rodrigues(rvec)
    p_meas = (-R_cam.T @ tvec).flatten()
    R_meas = R_cam.T @ CV_FROM_BODY
    assert np.allclose(p_meas, p, atol=1e-3), (p_meas, p)
    assert np.allclose(R_meas, R, atol=1e-4), (R_meas, R)


def test_plane_depths_are_uniform_in_inverse_depth():
    depths, inv = ds._plane_depths(60.0, 120.0, max_baseline=10.0, fx=K[0, 0])
    assert np.allclose(np.diff(inv), np.diff(inv)[0]), "planes must be evenly spaced in 1/Z"
    assert np.allclose(depths, 1.0 / inv)
    assert depths[0] > depths[-1] and np.isclose(depths[-1], 60.0) and np.isclose(depths[0], 120.0)
    # Spacing fine enough that consecutive planes are sub-pixel in
    # disparity - unless MAX_PLANES caps it first, which is a deliberate
    # cost control rather than a geometry error. Either way it must stay
    # inside a pixel, which is what the sub-plane parabolic refinement in
    # _extract_depth needs to be meaningful.
    step_px = K[0, 0] * 10.0 * np.diff(inv)[0]
    assert step_px <= ds.TARGET_DISPARITY_STEP_PX + 1e-9 or len(depths) == ds.MAX_PLANES
    assert step_px < 1.0, step_px


def test_depth_range_from_sparse():
    R, p = _sample_pose(10)
    rng = np.random.default_rng(11)
    u = rng.uniform(50, 900, 400)
    v = rng.uniform(50, 500, 400)
    d = rng.uniform(70.0, 95.0, 400)
    pts = ds._pixel_to_world_at_depth(u, v, d, R, p, K)
    near, far = ds.depth_range_from_sparse(pts, R, p, K, (540, 960))
    assert near < 72.0 and far > 93.0, (near, far)
    assert near > ds.MIN_DEPTH_M and far < ds.MAX_DEPTH_M
    assert ds.depth_range_from_sparse(pts[:3], R, p, K, (540, 960)) is None


def test_recover_relative_pose_roundtrip():
    """tracking.Tracker._relative_reinit's whole premise: cv2.recoverPose's
    camera-frame relative (R, t) can be converted into this repo's (R, p)
    body-to-world convention and anchored at a known reference pose,
    recovering the second camera's absolute pose exactly (up to the scale
    ambiguity any monocular two-view method has - resolved here with a
    known ground-truth baseline, and in production with a short-window IMU
    displacement magnitude; see _relative_reinit).

    This was derived by composing camera-frame transforms and verified
    empirically (not trusted from OpenCV's docs) across 200 random
    configurations before ever being used - worst case 1.7e-6 deg
    rotation error, 2e-8 m position error. This test is the permanent,
    lightweight record of that check."""
    R_ref, p_ref = _sample_pose(20)
    R_cur, p_cur = _sample_pose(21)
    rng = np.random.default_rng(22)
    # Points roughly in front of both cameras, indoor-ish range.
    pts3d = p_ref[None, :] + rng.normal(scale=1.5, size=(60, 3)) - R_ref[:, 2] * 3.0

    P_ref = build_projection_matrix(K, R_ref, p_ref)
    P_cur = build_projection_matrix(K, R_cur, p_cur)

    def project(P, X):
        h = P @ np.append(X, 1.0)
        return h[:2] / h[2]

    pts_ref = np.array([project(P_ref, X) for X in pts3d])
    pts_cur = np.array([project(P_cur, X) for X in pts3d])

    E, mask = cv2.findEssentialMat(pts_cur, pts_ref, K, method=cv2.RANSAC,
                                   threshold=1.0, prob=0.999)
    _, R_rel, t_rel, _ = cv2.recoverPose(E, pts_cur, pts_ref, K, mask=mask)

    true_scale = np.linalg.norm(p_cur - p_ref)
    R_cur_rec, p_cur_rec = recover_relative_pose(
        R_ref, p_ref, R_rel, t_rel.flatten() * true_scale)

    err_R_deg = np.degrees(np.arccos(
        np.clip((np.trace(R_cur.T @ R_cur_rec) - 1) / 2, -1, 1)))
    err_p_m = np.linalg.norm(p_cur_rec - p_cur)
    assert err_R_deg < 1e-3, err_R_deg
    assert err_p_m < 1e-4, err_p_m


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
