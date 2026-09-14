"""
SO(3) math helpers, shared by tracking.py and live_pipeline.py.

Split out of inertial_nav_ekf.py (removed - see CHANGELOG.md "Removed the
EKF/IMU-dead-reckoning trajectory"): these are pure rotation-math functions
with no filter state, so they belong on their own rather than inside the
class that used to own them.
"""
import numpy as np


def skew(v):
    return np.array([
        [0, -v[2], v[1]],
        [v[2], 0, -v[0]],
        [-v[1], v[0], 0],
    ])


def axang_to_R(dtheta):
    """so(3) -> SO(3) exponential map (Rodrigues' formula)."""
    angle = np.linalg.norm(dtheta)
    if angle < 1e-12:
        return np.eye(3) + skew(dtheta)  # first-order approx, avoids /0
    axis = dtheta / angle
    K = skew(axis)
    return np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * (K @ K)


def R_to_axang(R):
    """SO(3) -> so(3) logarithm map. Inverse of axang_to_R, used to turn a
    measured/reference rotation into a small-angle error vector against a
    current attitude estimate."""
    cos_angle = np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0)
    angle = np.arccos(cos_angle)
    if angle < 1e-8:
        return np.zeros(3)
    axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * np.sin(angle))
    return axis * angle


def level_and_align_attitude(accel_first_body, course_heading_rad=None,
                              camera_yaw_offset_rad=0.0):
    """One-shot initial attitude from a single (near-static) accelerometer
    sample plus, optionally, a GPS-course yaw observation. NOT a filter -
    this runs once at startup, the same leveling math
    InertialNavEKF.initialize_attitude used before the EKF was removed (see
    CHANGELOG.md). Kept because there is no other source for the camera's
    initial roll/pitch, and - when course_heading_rad is given - yaw: it is
    a one-time geometric computation, not ongoing IMU dead-reckoning.

    Leveling from accelerometer: at rest the sensed specific force is
    ~[0,0,+G] in world axes, so aligning the measured direction to world +Z
    recovers pitch/roll. Yaw is not observable from the accelerometer at
    all; course_heading_rad (a moving vehicle's course over ground,
    differenced from a few seconds of GPS fixes - see
    georeference.course_over_ground) supplies it when available. Passing
    None keeps the old accelerometer-only behaviour (correct for a
    genuinely stationary start, where course over ground is undefined) -
    measured on this project's data as ~72 degrees out and, since nothing
    downstream observes yaw either, never corrected.

    camera_yaw_offset_rad is the mounting angle between the camera's
    image-up axis (+R[:,1]) and the vehicle's direction of travel; 0 means
    a forward-tilted nadir camera whose image-up points along the flight
    path.
    """
    b = np.array(accel_first_body)
    b = b / np.linalg.norm(b)
    w = np.array([0, 0, 1])
    axis = np.cross(b, w)
    s = np.linalg.norm(axis)
    c = np.dot(b, w)
    if s < 1e-8:
        R = np.eye(3)
    else:
        axis = axis / s
        angle = np.arctan2(s, c)
        R = axang_to_R(axis * angle)

    if course_heading_rad is not None:
        # Rotate about world Z until the camera's image-up axis points
        # along the course. Leveling already fixed roll/pitch and a
        # world-Z rotation cannot disturb them, so the two stages compose
        # without interfering.
        up_axis = R[:, 1]
        current = np.arctan2(up_axis[1], up_axis[0])
        desired = course_heading_rad + camera_yaw_offset_rad
        R = axang_to_R(np.array([0.0, 0.0, desired - current])) @ R
        U, _, Vt = np.linalg.svd(R)
        R = U @ Vt
    return R


def umeyama_alignment(src, dst, with_scale=True):
    """Least-squares similarity transform (rotation R, translation t, scale
    s) mapping `src` points onto `dst` points in the sense of minimizing
    sum ||s*R@src_i + t - dst_i||^2 (Umeyama, 1991 - the same closed-form
    SVD solution ORB-SLAM/evo use for trajectory alignment).

    Added for the VGGT batch pipeline (see docs/dev_notes for the pivot
    away from the removed EKF): VGGT's predicted camera centers live in an
    arbitrary per-window frame (origin/scale/orientation set by the model,
    not geography), so georeferencing means fitting this transform between
    VGGT's camera-center trajectory and the GPS-derived ENU positions for
    the same frames, then applying it to that window's whole point cloud -
    the batch-mode equivalent of what course_over_ground/GPS-reanchoring
    did for the old per-frame pose.

    src, dst: (N,3) arrays, N>=3 and not collinear for a well-posed R.
    with_scale=False pins scale to 1 (use when src is already known-metric
    and only orientation/origin are unknown).

    Returns (s, R, t) such that dst ~= s * (R @ src.T).T + t.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n = src.shape[0]
    mu_src, mu_dst = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - mu_src, dst - mu_dst

    cov = (dst_c.T @ src_c) / n
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1  # reflection correction, keeps R a proper rotation
    R = U @ S @ Vt

    if with_scale:
        var_src = (src_c ** 2).sum() / n
        s = float(np.trace(np.diag(D) @ S) / var_src) if var_src > 1e-12 else 1.0
    else:
        s = 1.0

    t = mu_dst - s * (R @ mu_src)
    return s, R, t


def rigid_from_pose_pair(R_dst, p_dst, R_new, p_new):
    """Closed-form rigid transform (R, t; scale fixed at 1) mapping a SINGLE
    camera pose (R_new, p_new) onto its known pose in another frame
    (R_dst, p_dst) - the same physical camera, seen in two different
    coordinate frames.

    Added for the VGGT multi-window pipeline's GPS-less case: with no GPS,
    umeyama_alignment's >=3-point requirement would force a large window
    overlap (expensive - see vggt_reconstruct.py's WINDOW_OVERLAP). But a
    full 6-DOF camera pose (rotation AND position, not just position) is
    itself already 6 constraints - exactly enough to pin a rigid transform
    - so a single shared frame between consecutive windows is sufficient
    when its full pose is used, not just its position. This is what lets
    WINDOW_OVERLAP stay at 1 instead of needing >=3.

    R_dst/R_new: 3x3 cam-to-world rotations of the SAME camera in the
    destination/new frame. p_dst/p_new: its 3-vector position in each.
    Returns (R, t) such that R @ p_new + t == p_dst and R @ R_new == R_dst.

    Scale is fixed at 1 rather than estimated (unlike umeyama_alignment)
    because a single point pair has no second distance to form a ratio
    from - this assumes VGGT's per-window scale is self-consistent (its
    README's "metric" claim), which is unverified across window boundaries
    and is the main known weakness of this chaining approach: any real
    per-window scale drift accumulates uncorrected, the same failure shape
    as uncorrected IMU dead-reckoning (see CHANGELOG.md) - just for scale
    instead of position. Worth re-measuring once real GPS is available to
    cross-check, per the plan's checkpoints.
    """
    R = R_dst @ R_new.T
    t = p_dst - R @ p_new
    return R, t
