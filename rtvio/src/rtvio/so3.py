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
