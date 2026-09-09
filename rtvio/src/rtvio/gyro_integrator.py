"""
Minimal gyro-only rotation integrator.

Replaces InertialNavEKF's role of supplying tracking.py's
windowed_bundle_adjustment with gyro_delta_R (see CHANGELOG.md "Removed the
EKF/IMU-dead-reckoning trajectory"). That prior is tracking.py's own
documented depth-accuracy fix (dense stereo's depth error from a relative
attitude error is Z^2 * dtheta / baseline - see tracking.py's
BA_REL_PRIOR_ANG_RAD comment), so it is kept; nothing else the old EKF did
(position/velocity dead-reckoning, accel-bias estimation, GPS fusion, ZUPT,
a 15x15 covariance) is.

Deliberately NOT a filter: no bias state, no covariance, no correction step.
It only ever integrates raw gyro samples between two points in time and
hands back the resulting rotation delta - a stopwatch, not an estimator.
Camera pose itself comes entirely from vision (tracking.py's PnP each
frame) and GPS (a direct re-anchor on each fix), never from this.
"""
import numpy as np

from .so3 import axang_to_R


class GyroIntegrator:
    def __init__(self):
        self.delta_R = np.eye(3)
        self.prev_t = None

    def add_sample(self, gyro, t_s):
        """Fold one IMU sample's gyro reading into the accumulated delta.
        Skips (rather than integrates across) a timestamp gap - a stale or
        negative dt here would inject an unmodelled rotation, and the first
        sample after any gap has no valid dt at all."""
        if self.prev_t is None:
            self.prev_t = t_s
            return
        dt = t_s - self.prev_t
        self.prev_t = t_s
        if dt <= 0 or dt > 0.5:
            return   # reconnect/stall gap - see live_pipeline.py's identical guard
        self.delta_R = self.delta_R @ axang_to_R(np.asarray(gyro) * dt)

    def consume(self):
        """Return the rotation accumulated since the last consume() (or
        construction) and reset to identity - the caller (live_pipeline.py,
        once per processed frame) owns exactly one delta per frame."""
        d = self.delta_R
        self.delta_R = np.eye(3)
        return d
