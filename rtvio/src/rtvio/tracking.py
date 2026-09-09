"""
Sparse visual front end: replaces live_visual_inertial_mapper.py.

Why this is structured differently from the old mug-demo version:
the old code re-detected ORB features every frame and matched them only
against the IMMEDIATELY PREVIOUS frame, then required
baseline(prev_frame, curr_frame) >= MIN_BASELINE before triangulating.
At aerial scale that baseline check can never pass: a UAV moving 10 m/s at
30fps covers ~0.33m between consecutive frames, so two-consecutive-frame
baseline is always ~0.33m regardless of which pair you look at - nowhere
near enough parallax to triangulate a point 80m away. Triangulation would
have silently never fired.

This version keeps persistent point *tracks* across many frames using
Lucas-Kanade optical flow (cheap, robust to the small per-frame motion
that breaks independent ORB matching), and only triangulates a track once
its ACCUMULATED baseline (first-seen pose vs. current pose, not
consecutive-frame pose) clears MIN_BASELINE - typically ~15-20 frames in,
by which point there's enough real parallax.

Also drops the old code's fake "Gaussian splat" PLY fields (scale/rot/
opacity heuristics that were never optimized against any rendering loss -
see rtvio_3/PROJECT_CONTEXT.md 2.1) in favor of a plain colored point
cloud; dense_stereo.py is what actually produces the dense reconstruction
the PS asks for, this module's job is pose refinement.
"""
import numpy as np
import cv2
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix
from .so3 import axang_to_R, R_to_axang


# Camera-axis convention. Poses (R, p) everywhere in this repo are the
# EKF's / the dataset's *Blender camera* convention: R is body-to-world,
# the camera looks along -R[:,2] and its image "up" is +R[:,1]. OpenCV's
# projection convention instead looks along +Z with image "down" as +Y.
# The two differ by a 180-degree rotation about the camera's X axis:
#   X_cv = S @ R.T @ (X_world - p),   S = diag(1, -1, -1)
# S is its own inverse and has det +1, so it is a genuine rotation and can
# be composed with camera rotations freely. Every place this file crosses
# between the two conventions (the projection matrix below, the bundle-
# adjustment residual, and solvePnPRansac's output) must apply it - an
# earlier version applied it in none of them, which silently mirrored
# every projection about the principal point and left the PnP attitude
# measurement 180 degrees out, so the EKF's innovation gate rejected
# essentially every vision update.
CV_FROM_BODY = np.diag([1.0, -1.0, -1.0])


def build_projection_matrix(K, R, C):
    """World-to-pixel projection matrix for a camera at position C with
    body-to-world rotation R (so R.T is world-to-body), converted into
    OpenCV's camera axes via CV_FROM_BODY."""
    Rcv = CV_FROM_BODY @ R.T
    tcv = -Rcv @ C
    return K @ np.hstack((Rcv, tcv.reshape(3, 1)))


def recover_relative_pose(R_ref, p_ref, R_rel, t_rel_scaled):
    """Convert OpenCV's cv2.recoverPose(E, pts_cur, pts_ref, K) output
    (camera-frame relative rotation/translation between two views) into
    this project's (R, p) body-to-world convention, anchored at a
    reference camera whose own (R_ref, p_ref) is already known.

    `t_rel_scaled` must already be metric (recoverPose's own t is unit
    length - the caller supplies the scale, e.g. from a short-window IMU
    displacement; see Tracker._relative_reinit).

    Empirically verified against build_projection_matrix/CV_FROM_BODY
    (this file's own conventions) over 200 random configurations: worst
    case 1.7e-6 deg rotation error, 2e-8 m position error - see
    tests/test_geometry.py's test_recover_relative_pose_roundtrip. Derived
    by composing camera-frame transforms rather than trusted from
    documentation, per this file's own rule about silent convention bugs.
    """
    R_cur = R_ref @ CV_FROM_BODY @ R_rel @ CV_FROM_BODY
    p_cur = p_ref + R_ref @ CV_FROM_BODY @ t_rel_scaled
    return R_cur, p_cur


def triangulate_point(P1, P2, pt1, pt2):
    return triangulate_multiview([P1, P2], [pt1, pt2])


def triangulate_multiview(Ps, pts):
    """DLT triangulation from ANY number of views: stack two rows per
    observation and take the null space.

    Using every view a track was seen in, rather than just its first and
    current one, is the difference between a usable map and an unusable
    one here. A triangulated point's depth error is roughly
    Z * sigma_pose / baseline, so at Z ~ 85 m with ~1.5 m of independent
    pose error at each end and the 4 m minimum baseline this tracker
    triangulates at, a two-view point is tens of metres out - measured on
    this dataset as a 1.45 m pose error turning into a 24 m error in the
    pose that solvePnPRansac then recovers from the resulting map. Every
    additional view both lengthens the effective baseline and averages
    down the pose noise.
    """
    rows = []
    for P, (u, v) in zip(Ps, pts):
        rows.append(u * P[2, :] - P[0, :])
        rows.append(v * P[2, :] - P[1, :])
    A = np.asarray(rows)
    # Row-normalize so views don't get weighted by their pixel magnitudes.
    norms = np.linalg.norm(A, axis=1, keepdims=True)
    A = A / np.where(norms < 1e-12, 1.0, norms)
    _, _, V = np.linalg.svd(A)
    Xh = V[-1, :]
    if abs(Xh[3]) < 1e-12:
        return np.full(3, np.nan)
    return Xh[0:3] / Xh[3]


class Tracker:
    MIN_ACTIVE_TRACKS = 400          # replenish with fresh ORB below this
    MAX_ACTIVE_TRACKS = 1200
    MIN_BASELINE_M = 4.0             # accumulated, not frame-to-frame
    MAX_DEPTH_M = 250.0              # aerial scale, not the mug demo's 20m
    MIN_DEPTH_M = 5.0
    NEW_FEATURE_MIN_DIST_PX = 12     # keeps ORB from redetecting existing tracks
    MIN_TRIANGULATION_VIEWS = 4      # views required before a track becomes a
                                     # map point (two-view points at this
                                     # baseline-to-depth ratio are unusable -
                                     # see triangulate_multiview)
    TRACK_VIEW_STRIDE = 3            # keep every Nth frame's observation;
                                     # consecutive frames add ~0.33 m of
                                     # baseline and almost no new information
    MAX_TRACK_VIEWS = 24
    RETRIANGULATE_EVERY_N_VIEWS = 6  # refresh an existing map point once it
                                     # has gathered this many more views
    BA_WINDOW = 10                   # frames per windowed bundle adjustment
    BA_MIN_SHARED_POINTS = 6
    BA_MAX_POINTS = 250              # cap on free points per BA solve; see
                                     # windowed_bundle_adjustment for why the
                                     # problem size matters so much here

    # Periodic two-view relative-pose reconstruction (_relative_reinit),
    # decoupled from the long-term-drifting absolute pose the incremental
    # path above depends on. See _relative_reinit's docstring for why this
    # exists: without GPS, MIN_BASELINE_M/MIN_DEPTH_M/MAX_DEPTH_M were
    # measured correctly rejecting every incremental triangulation attempt
    # on a real session where the absolute pose had drifted 24 m over 49 s
    # of a physically few-metre motion - not a bug, but it meant zero
    # points ever got through. This path uses PURE 2D correspondence
    # (Essential matrix) for geometry, which does not degrade with elapsed
    # time the way a dead-reckoned position would, and only a SHORT-WINDOW
    # displacement of the caller's own pose estimate (vision, snapped to
    # GPS on each fix - see live_pipeline.py) for scale.
    # Gated on ELAPSED REAL TIME, not processed-frame count: a frame-count
    # stride (an earlier version of this) is blind to processing/network
    # congestion, and this project's whole machine has plenty of that
    # (measured: frame gaps, late-dropped events, sub-real-time throughput
    # every session so far). During a congested stretch, N processed frames
    # can span far more real time than intended - long enough that the
    # caller's own short-window position delta (this method's ONLY source
    # of scale) is no longer trustworthy. Measured consequence on a real
    # session (under the old EKF-driven pose): reconstruction was fine
    # (bounded, ~5m) through t=34s, then diverged to 361m by t=54s -
    # consistent with a bad-scale reinit during a congested stretch
    # poisoning a subsequent PnP-based vision correction.
    REINIT_MIN_WINDOW_S = 0.5        # too short: not enough real parallax
                                     # to rise above pixel/sensor noise
    REINIT_MAX_WINDOW_S = 2.0        # too long: the position delta used for
                                     # scale is no longer short-term-
                                     # trustworthy: refresh the snapshot and
                                     # skip reconstructing from this window
    MIN_ESSENTIAL_MATCHES = 30       # correspondences needed for a robust E
    MIN_ESSENTIAL_INLIERS = 20       # recoverPose inliers required to trust it
    ESSENTIAL_RANSAC_PX = 1.5        # findEssentialMat inlier threshold
    MIN_REINIT_SCALE_M = 0.05        # ignore a window the caller's own pose
                                     # barely moved in - too little motion
                                     # for a trustworthy scale

    def __init__(self, K, run_bundle_adjustment=True):
        self.K = np.array(K, dtype=np.float64)
        self.run_ba = run_bundle_adjustment

        self.orb = cv2.ORB_create(nfeatures=3000, scaleFactor=1.2, nlevels=8,
                                   edgeThreshold=15, fastThreshold=10)
        self.lk_params = dict(winSize=(21, 21), maxLevel=3,
                               criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))

        self.prev_gray = None
        # Per-active-track parallel arrays/lists (index-aligned):
        self.active_px = np.zeros((0, 2), dtype=np.float32)
        self.first_px = []       # pixel position when track was born
        self.first_R = []        # camera R when track was born
        self.first_p = []        # camera position when track was born
        self.map_idx = []        # index into self.points once triangulated, else -1
        self.track_id = []       # persistent identity, survives array reindexing -
                                 # needed to match "this same feature" between two
                                 # non-adjacent frames for _relative_reinit
        self.next_track_id = 0
        self._ref_snapshot = None   # (t_s, active_px, track_id, R, p) - see
                                    # _relative_reinit
        # Full observation history per ACTIVE track: list of (R, p, px) at
        # every frame the track has been seen in, subsampled by
        # TRACK_VIEW_STRIDE. This is what lets a track be triangulated from
        # all its views (see triangulate_multiview) instead of just its
        # first and current one, and re-triangulated as it accumulates more.
        self.track_views = []

        self.points = []          # triangulated 3D map points
        self.colors = []
        self.obs_count = []
        self.observations = []    # per map point: list of (frame_idx, px, py)

        self.frame_idx = 0
        self.cam_trail = []
        self.pose_history = []    # (frame_idx, R, p) for the BA window

    # ---------------------------------------------------------- helpers --
    def _detect_new_features(self, gray):
        mask = np.full(gray.shape, 255, dtype=np.uint8)
        for (x, y) in self.active_px:
            cv2.circle(mask, (int(x), int(y)), self.NEW_FEATURE_MIN_DIST_PX, 0, -1)
        kps = self.orb.detect(gray, mask)
        if not kps:
            return np.zeros((0, 2), dtype=np.float32)
        pts = np.array([kp.pt for kp in kps], dtype=np.float32)
        room = self.MAX_ACTIVE_TRACKS - len(self.active_px)
        if room <= 0:
            return np.zeros((0, 2), dtype=np.float32)
        if len(pts) > room:
            # prefer the strongest corners if we have more candidates than room
            responses = np.array([kp.response for kp in kps])
            keep = np.argsort(-responses)[:room]
            pts = pts[keep]
        return pts

    def _triangulate_views(self, views, curr_R, curr_p, curr_px):
        """Triangulate one track from all of its recorded views plus the
        current one. Returns the world point, or None if the track fails
        the baseline / view-count / depth / reprojection checks."""
        all_views = list(views) + [(curr_R, curr_p, np.asarray(curr_px, dtype=np.float64))]
        if len(all_views) < self.MIN_TRIANGULATION_VIEWS:
            return None
        centers = np.array([v[1] for v in all_views])
        # Widest separation between any two of the views, which is the
        # parallax actually available - not just first-to-current.
        baseline = np.linalg.norm(centers[:, None, :] - centers[None, :, :], axis=-1).max()
        if baseline < self.MIN_BASELINE_M:
            return None

        Ps = [build_projection_matrix(self.K, R, p) for R, p, _ in all_views]
        X = triangulate_multiview(Ps, [px for _, _, px in all_views])
        if not np.all(np.isfinite(X)):
            return None

        for R, p, px in all_views:
            depth = -(X - p) @ R[:, 2]
            if not (self.MIN_DEPTH_M < depth < self.MAX_DEPTH_M):
                return None
        # Reprojection consistency: a point that cannot be projected back
        # onto its own observations to within a few pixels is a bad match
        # or a bad pose, and letting it into the map poisons both the PnP
        # measurement and the dense stage's depth prior.
        errs = []
        for P, (_R, _p, px) in zip(Ps, all_views):
            h = P @ np.append(X, 1.0)
            if h[2] <= 1e-6:
                return None
            errs.append(np.linalg.norm(h[:2] / h[2] - px))
        if np.mean(errs) > self.MAX_TRIANGULATION_REPROJ_PX:
            return None
        return X

    # Outlier gate, NOT a precision gate. A perfectly-triangulated point
    # still reprojects several pixels off here, because the poses feeding
    # the triangulation are themselves ~1.5 m uncertain and at 85 m depth
    # that is 1.5/85 rad = ~11 px of apparent motion. Setting this near the
    # precision one would like (3 px) rejects essentially every point and
    # empties the map - observed as the map collapsing from ~470 points to
    # 3. What it is here to catch is a track that optical flow has slid onto
    # a different surface, which shows up as tens of pixels, not ten.
    MAX_TRIANGULATION_REPROJ_PX = 25.0

    def _try_triangulate(self, i, curr_R, curr_p, curr_px, img):
        X = self._triangulate_views(self.track_views[i], curr_R, curr_p, curr_px)
        if X is None:
            return None
        px, py = int(round(curr_px[0])), int(round(curr_px[1]))
        if not (0 <= px < img.shape[1] and 0 <= py < img.shape[0]):
            return None
        color = img[py, px, :].astype(float)[::-1]
        return X, color

    # ----------------------------------------------------------- main API --
    def process_frame(self, img, curr_R, curr_p, gyro_delta_R=None, t_s=None):
        """curr_R/curr_p: the caller's current pose estimate for this frame
        - live_pipeline.py's own running vision (PnP) pose, snapped to GPS
        on each fix; see CHANGELOG.md "Removed the EKF/IMU-dead-reckoning
        trajectory". This module does not run its own filter, it just needs
        somewhere to triangulate from and something to correct via
        solvePnPRansac.

        t_s: this frame's own session-relative timestamp, off the wire -
        NOT wall-clock processing time, which can lag it arbitrarily under
        congestion. Used only by _relative_reinit to gate on real elapsed
        time rather than processed-frame count; see its class-level
        REINIT_MIN/MAX_WINDOW_S comment for why that distinction matters.
        Optional for backward compatibility, but the live caller always
        provides it.

        gyro_delta_R: the rotation GyroIntegrator accumulated between the
        previous frame and this one - raw gyro integration, no filter, no
        GPS/vision correction folded in - i.e. the gyro's own answer for
        the relative rotation. Optional; see windowed_bundle_adjustment for
        what it is worth."""
        self.frame_idx += 1
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        self.cam_trail.append(curr_p.copy())

        num_new_points = 0
        num_reobserved = 0
        pnp_result = None

        if self.prev_gray is not None and len(self.active_px) > 0:
            next_px, status, _ = cv2.calcOpticalFlowPyrLK(
                self.prev_gray, gray, self.active_px, None, **self.lk_params
            )
            status = status.reshape(-1).astype(bool)
            in_bounds = (
                (next_px[:, 0] >= 0) & (next_px[:, 0] < gray.shape[1]) &
                (next_px[:, 1] >= 0) & (next_px[:, 1] < gray.shape[0])
            )
            keep = status & in_bounds

            new_active_px, new_first_px, new_first_R, new_first_p, new_map_idx = [], [], [], [], []
            new_track_views, new_track_id = [], []
            valid_3d, valid_2d = [], []

            for i in range(len(self.active_px)):
                if not keep[i]:
                    continue
                px = next_px[i]

                if self.map_idx[i] < 0:
                    result = self._try_triangulate(i, curr_R, curr_p, px, img)
                    if result is not None:
                        X, color = result
                        self.points.append(X)
                        self.colors.append(color)
                        self.obs_count.append(1)
                        self.observations.append([(self.frame_idx, px[0], px[1])])
                        new_map_idx.append(len(self.points) - 1)
                        num_new_points += 1
                    else:
                        new_map_idx.append(-1)
                else:
                    m = self.map_idx[i]
                    # Re-triangulate an existing map point once its track has
                    # gathered more views: the extra parallax and the extra
                    # averaging of pose noise both tighten it, and a point
                    # first triangulated at the minimum baseline is the
                    # loosest one in the map.
                    nviews = len(self.track_views[i])
                    if nviews >= self.MIN_TRIANGULATION_VIEWS and                             nviews % self.RETRIANGULATE_EVERY_N_VIEWS == 0:
                        X_new = self._triangulate_views(self.track_views[i], curr_R, curr_p, px)
                        if X_new is not None:
                            self.points[m] = X_new
                    n = self.obs_count[m]
                    newc = img[int(px[1]), int(px[0]), :].astype(float)[::-1] if \
                        0 <= int(px[0]) < img.shape[1] and 0 <= int(px[1]) < img.shape[0] else self.colors[m]
                    self.colors[m] = (self.colors[m] * n + newc) / (n + 1)
                    self.obs_count[m] = n + 1
                    self.observations[m].append((self.frame_idx, px[0], px[1]))
                    valid_3d.append(self.points[m])
                    valid_2d.append(px)
                    num_reobserved += 1
                    new_map_idx.append(m)

                new_active_px.append(px)
                new_first_px.append(self.first_px[i])
                new_first_R.append(self.first_R[i])
                new_first_p.append(self.first_p[i])
                new_track_id.append(self.track_id[i])
                views = self.track_views[i]
                if len(views) < self.MAX_TRACK_VIEWS and                         (self.frame_idx % self.TRACK_VIEW_STRIDE == 0 or not views):
                    views = views + [(curr_R.copy(), curr_p.copy(), np.array(px, dtype=np.float64))]
                new_track_views.append(views)

            self.active_px = np.array(new_active_px, dtype=np.float32) if new_active_px else np.zeros((0, 2), dtype=np.float32)
            self.first_px, self.first_R, self.first_p, self.map_idx = new_first_px, new_first_R, new_first_p, new_map_idx
            self.track_views = new_track_views
            self.track_id = new_track_id

            if len(valid_3d) >= 6:
                valid_3d = np.array(valid_3d, dtype=np.float32)
                valid_2d = np.array(valid_2d, dtype=np.float32)
                ok, rvec, tvec, inliers = cv2.solvePnPRansac(
                    valid_3d, valid_2d, self.K, None, reprojectionError=4.0, confidence=0.99
                )
                if ok and inliers is not None and len(inliers) >= 6:
                    R_cam, _ = cv2.Rodrigues(rvec)   # world -> OpenCV camera
                    p_meas = (-R_cam.T @ tvec).flatten()
                    # R_cam == CV_FROM_BODY @ R.T, so R == (CV_FROM_BODY @ R_cam).T
                    R_meas = R_cam.T @ CV_FROM_BODY
                    pnp_result = (p_meas, R_meas, len(inliers))

        if len(self.active_px) < self.MIN_ACTIVE_TRACKS:
            new_pts = self._detect_new_features(gray)
            for pt in new_pts:
                self.first_px.append(pt)
                self.first_R.append(curr_R.copy())
                self.first_p.append(curr_p.copy())
                self.map_idx.append(-1)
                self.track_id.append(self.next_track_id)
                self.next_track_id += 1
                self.track_views.append([(curr_R.copy(), curr_p.copy(), np.array(pt, dtype=np.float64))])
            if len(new_pts) > 0:
                self.active_px = np.vstack([self.active_px, new_pts]) if len(self.active_px) else new_pts

        num_new_points += self._relative_reinit(curr_R, curr_p, img, t_s)

        self.pose_history.append((self.frame_idx, curr_R.copy(), curr_p.copy(),
                                  None if gyro_delta_R is None else np.array(gyro_delta_R, dtype=np.float64)))
        if len(self.pose_history) > self.BA_WINDOW:
            self.pose_history.pop(0)

        self.prev_gray = gray

        return {
            "frame_idx": self.frame_idx,
            "num_new_points": num_new_points,
            "num_reobserved": num_reobserved,
            "total_points": len(self.points),
            "active_tracks": len(self.active_px),
            "pnp": pnp_result,
        }

    # ------------------------------------------ relative-pose reinit path --
    def _relative_reinit(self, curr_R, curr_p, img, t_s=None):
        """Periodic two-view relative reconstruction, decoupled from the
        long-term-drifting absolute pose the incremental path above
        depends on (see MIN_BASELINE_M's class-level comment for the
        measured 24m/49s failure this addresses).

        Every REINIT_MIN_WINDOW_S to REINIT_MAX_WINDOW_S of REAL elapsed
        time (t_s, off the wire - not wall-clock processing time), matches
        the current active tracks (by persistent track_id, not array
        position - positions shift as tracks are lost) against a snapshot
        taken at the window's start, recovers relative pose PURELY from
        that 2D correspondence (Essential matrix - unaffected by how much
        the caller's own pose estimate has drifted since), and scales it
        using only the MAGNITUDE of that pose's change over this short
        window - short enough that drift hasn't had time to dominate it,
        and using only the magnitude (not the vector) sidesteps a wrong yaw
        entirely, since that only rotates the direction, not the speed.

        Gated on REAL TIME, not processed-frame count, because this
        project's whole machine runs under real congestion (frame gaps,
        late-dropped events, sub-real-time throughput, every session so
        far) - a frame-count stride is blind to that: during a congested
        stretch, N processed frames can span far more real time than
        intended, long enough that the position delta this method relies
        on for scale is no longer trustworthy. Measured consequence of the
        frame-count version on a real session (under the old EKF-driven
        pose): reconstruction was fine (bounded, ~5m) through t=34s, then
        diverged to 361m by t=54s - consistent with a bad-scale reinit
        during a congested stretch poisoning a later PnP-based vision
        correction. If t_s is not provided, this is disabled outright
        (returns 0) rather than falling back to the frame-count version
        that produced that failure.

        Honest limitation: each reinit window is anchored to the caller's
        OWN pose estimate at snapshot time (vision, snapped to GPS on each
        fix - see live_pipeline.py), so consecutive windows can still drift
        relative to EACH OTHER over the session - this fixes local
        geometric consistency (a window's own points are correctly shaped
        and scaled), not global consistency across the whole session, which
        needs GPS or loop closure to ever be exact.
        """
        if t_s is None:
            return 0
        if self._ref_snapshot is None:
            self._ref_snapshot = (t_s, self.active_px.copy(),
                                  list(self.track_id), curr_R.copy(), curr_p.copy())
            return 0
        ref_t, ref_px, ref_ids, ref_R, ref_p = self._ref_snapshot
        elapsed = t_s - ref_t
        if elapsed < self.REINIT_MIN_WINDOW_S:
            return 0
        # Refresh the snapshot unconditionally past this point, win or lose -
        # a bad window (too little motion, too few matches, too much real
        # time elapsed) must not wedge every future attempt against a stale
        # reference forever.
        new_points = 0
        scale = np.linalg.norm(curr_p - ref_p)
        if elapsed <= self.REINIT_MAX_WINDOW_S and scale >= self.MIN_REINIT_SCALE_M and len(ref_ids):
            id_to_ref_px = {tid: px for tid, px in zip(ref_ids, ref_px)}
            id_to_map_idx = dict(zip(self.track_id, self.map_idx))
            id_to_pos = {tid: i for i, tid in enumerate(self.track_id)}
            common_ids = [tid for tid in self.track_id if tid in id_to_ref_px]
            if len(common_ids) >= self.MIN_ESSENTIAL_MATCHES:
                pts_cur = np.array([self.active_px[id_to_pos[t]] for t in common_ids], dtype=np.float64)
                pts_ref = np.array([id_to_ref_px[t] for t in common_ids], dtype=np.float64)
                E, mask = cv2.findEssentialMat(pts_cur, pts_ref, self.K, method=cv2.RANSAC,
                                               threshold=self.ESSENTIAL_RANSAC_PX, prob=0.999)
                if E is not None and E.shape == (3, 3):
                    n_inliers, R_rel, t_rel, mask_pose = cv2.recoverPose(
                        E, pts_cur, pts_ref, self.K, mask=mask)
                    if n_inliers >= self.MIN_ESSENTIAL_INLIERS:
                        R_cur_est, p_cur_est = recover_relative_pose(
                            ref_R, ref_p, R_rel, t_rel.flatten() * scale)
                        P_ref = build_projection_matrix(self.K, ref_R, ref_p)
                        P_cur = build_projection_matrix(self.K, R_cur_est, p_cur_est)
                        inliers = (mask_pose.flatten().astype(bool) if mask_pose is not None
                                  else np.ones(len(common_ids), dtype=bool))
                        for k in np.where(inliers)[0]:
                            tid = common_ids[k]
                            if id_to_map_idx.get(tid, -1) != -1:
                                continue    # already mapped; leave refinement to the incremental path
                            X = triangulate_point(P_ref, P_cur, pts_ref[k], pts_cur[k])
                            if not np.all(np.isfinite(X)):
                                continue
                            depth_ref = -(X - ref_p) @ ref_R[:, 2]
                            depth_cur = -(X - p_cur_est) @ R_cur_est[:, 2]
                            if not (self.MIN_DEPTH_M < depth_ref < self.MAX_DEPTH_M
                                    and self.MIN_DEPTH_M < depth_cur < self.MAX_DEPTH_M):
                                continue
                            pos = id_to_pos[tid]
                            px_now = self.active_px[pos]
                            pxr, pyr = int(round(px_now[0])), int(round(px_now[1]))
                            if not (0 <= pxr < img.shape[1] and 0 <= pyr < img.shape[0]):
                                continue
                            color = img[pyr, pxr, :].astype(float)[::-1]
                            self.points.append(X)
                            self.colors.append(color)
                            self.obs_count.append(1)
                            self.observations.append([(self.frame_idx, px_now[0], px_now[1])])
                            self.map_idx[pos] = len(self.points) - 1
                            new_points += 1
        self._ref_snapshot = (t_s, self.active_px.copy(),
                              list(self.track_id), curr_R.copy(), curr_p.copy())
        return new_points

    # ------------------------------------------------- windowed bundle adj --
    # Reject a BA result that moves any pose in the window further than this
    # from the prior it was given. The soft priors below already make a large
    # excursion expensive, so this only ever fires on a genuinely degenerate
    # solve (a near-planar point set, or a window where optical flow has lost
    # almost every track) - cheap insurance, not the primary safeguard.
    MAX_BA_POSE_JUMP_M = 5.0

    # Soft-prior strengths for the windowed BA below, in the units of the
    # states they constrain, not free knobs: they are solvePnPRansac's own
    # typical accuracy (position from its inlier reprojection residual at
    # aerial-survey depth, attitude similarly), which is what the incoming
    # pose in this window actually is now that there is no EKF fusing it
    # with anything else (see CHANGELOG.md "Removed the EKF/IMU-dead-
    # reckoning trajectory") - so that is exactly how strongly BA should be
    # allowed to argue with it.
    BA_POSE_PRIOR_POS_M = 1.5
    BA_POSE_PRIOR_ANG_RAD = 0.05

    # How tightly consecutive poses are tied to the GYRO-propagated rotation
    # between them (live_pipeline.py's GyroIntegrator - raw integration, no
    # filter). Far tighter than the absolute attitude prior above, because
    # over a single frame interval it is a far better measurement:
    # integrating this dataset's raw gyro open-loop tracks ground truth to
    # 0.5 deg over the whole 20 s flight, whereas the incoming ABSOLUTE pose
    # is only as good as the last solvePnPRansac call (or, once every GPS
    # fix, a direct re-anchor that can itself be a discontinuous jump) -
    # either way a noisier source for exactly the relative rotation the
    # dense stage is most sensitive to.
    #
    # Why this particular constraint matters more than any other: dense
    # stereo's depth error from a relative attitude error d_theta is
    # Z^2 * d_theta / baseline. At Z = 85 m over a 15 m baseline, the
    # measured 2.3 deg of relative attitude error is ~19 m of depth error -
    # which is what the reconstruction was actually limited by, and why
    # lengthening the stereo baseline did not help (the relative attitude
    # error grows with the interval, cancelling the 1/baseline).
    BA_REL_PRIOR_ANG_RAD = 0.003

    def windowed_bundle_adjustment(self):
        """Refine the last BA_WINDOW camera poses + the map points they
        share, by minimizing reprojection error against soft priors on every
        pose. This is the pure numpy/scipy stand-in for what a GTSAM factor
        graph would do (GTSAM has no Windows wheel on this machine).

        GAUGE. Reprojection error alone does not determine where a window
        sits in the world: rigidly transforming every camera and every point
        together leaves every residual unchanged, so a pure-reprojection
        solve has 7 unconstrained directions (6 rigid + scale) and
        least_squares will happily wander along them - observed in an early
        version as the estimate blowing up past 1e40 metres.

        An earlier fix held pose 0 of the window FIXED. That removes the
        gauge freedom, but at a price: it discards what the IMU/GPS filter
        knows about all the OTHER poses, and it makes the whole window
        inherit pose 0's error exactly. Measured, feeding those results back
        to the filter made the trajectory an order of magnitude worse
        (ATE 2.2 m -> 24.9 m).

        This version instead adds an explicit PRIOR RESIDUAL for every pose
        in the window - position and attitude, weighted by solvePnPRansac's
        own typical accuracy (BA_POSE_PRIOR_POS_M/ANG_RAD; see their
        comment). That pins the gauge (with several non-collinear camera
        positions anchored, no rigid transform or rescaling leaves the priors
        unchanged) while still letting every pose move as far as the images
        justify. It is also simply the correct estimator: the priors carry
        the incoming pose's own information (vision, occasionally re-anchored
        to GPS), the reprojections carry the visual information from THIS
        window's images, and this is their joint MAP solution rather than an
        arbitrary choice between the two.

        Returns the refined window as [(frame_idx, R, p), ...], or None.
        """
        if not self.run_ba or len(self.pose_history) < 3:
            return None
        frame_idxs = [e[0] for e in self.pose_history]
        window_set = set(frame_idxs)

        shared = [(sum(1 for f, _, _ in obs if f in window_set), i)
                  for i, obs in enumerate(self.observations)]
        point_ids = [i for n_obs, i in shared if n_obs >= 2]
        if len(point_ids) < self.BA_MIN_SHARED_POINTS:
            return None
        if len(point_ids) > self.BA_MAX_POINTS:
            # Keep the best-constrained points rather than an arbitrary
            # prefix: a point seen in 8 of the window's frames pins the
            # geometry far better than one seen in 2.
            ranked = sorted(((n, i) for n, i in shared if n >= 2), reverse=True)
            point_ids = sorted(i for _, i in ranked[:self.BA_MAX_POINTS])

        n_poses = len(self.pose_history)
        pose0 = [(e[1].copy(), e[2].copy()) for e in self.pose_history]
        gyro_dR = [e[3] for e in self.pose_history]

        residual_specs = []  # (pose_local_idx, point_local_idx, px, py)
        pid_to_local = {pid: k for k, pid in enumerate(point_ids)}
        fidx_to_local = {f: k for k, f in enumerate(frame_idxs)}
        for pid in point_ids:
            for f, px, py in self.observations[pid]:
                if f in fidx_to_local:
                    residual_specs.append((fidx_to_local[f], pid_to_local[pid], px, py))
        if len(residual_specs) < 2 * len(point_ids):
            return None  # not enough constraints per point to be worth it

        # Every pose is free; the priors below, not a fixed pose, fix the gauge.
        x0 = []
        for _R, p in pose0:
            x0.extend([0.0, 0.0, 0.0])     # rotation delta (axis-angle) from R
            x0.extend(p.tolist())
        for pid in point_ids:
            x0.extend(np.array(self.points[pid]).tolist())
        x0 = np.array(x0, dtype=np.float64)
        n_pose_params = n_poses * 6

        def unpack_poses(x):
            poses = []
            for k in range(n_poses):
                base = k * 6
                R = pose0[k][0] @ axang_to_R(x[base:base + 3])
                poses.append((R, x[base + 3:base + 6]))
            return poses

        # Residual bookkeeping as flat arrays, so the residual function below
        # is pure numpy. least_squares finite-differences the Jacobian, i.e.
        # it calls this many times per iteration, so a Python loop over every
        # observation here is the difference between the BA stage costing
        # seconds and costing minutes.
        spec = np.array(residual_specs, dtype=np.float64)
        spec_pose = spec[:, 0].astype(np.int64)
        spec_pt = spec[:, 1].astype(np.int64)
        obs_uv = spec[:, 2:4]
        n_obs_res = len(residual_specs) * 2
        fx, fy = self.K[0, 0], self.K[1, 1]
        cx, cy = self.K[0, 2], self.K[1, 2]
        flip = np.array([1.0, -1.0, -1.0])   # diagonal of CV_FROM_BODY
        prior_p = np.array([p for _R, p in pose0])
        w_pos = 1.0 / self.BA_POSE_PRIOR_POS_M
        w_ang = 1.0 / self.BA_POSE_PRIOR_ANG_RAD

        rel_idx = [k for k in range(1, n_poses) if gyro_dR[k] is not None]
        rel_dR = [gyro_dR[k] for k in rel_idx]
        w_rel_ang = 1.0 / self.BA_REL_PRIOR_ANG_RAD
        n_rel_res = 3 * len(rel_idx)

        def residuals(x):
            poses = unpack_poses(x)
            pts = x[n_pose_params:].reshape(-1, 3)
            Rs = np.stack([R for R, _ in poses])
            Cs = np.stack([p for _, p in poses])

            d = pts[spec_pt] - Cs[spec_pose]                       # (N,3)
            Xc = np.einsum("nj,njk->nk", d, Rs[spec_pose]) * flip  # R.T @ d, then CV axes
            z = Xc[:, 2]
            in_front = z > 1e-3
            safe_z = np.where(in_front, z, 1.0)
            proj = np.stack([fx * Xc[:, 0] / safe_z + cx,
                             fy * Xc[:, 1] / safe_z + cy], axis=1)
            # Points behind a camera contribute no residual rather than an
            # arbitrarily large one, which would otherwise dominate the whole
            # solve on a single bad triangulation.
            obs_res = np.where(in_front[:, None], proj - obs_uv, 0.0).reshape(-1)

            # Prior residuals. The rotation delta IS the deviation from the
            # prior attitude, so it is its own residual.
            dtheta = x[:n_pose_params].reshape(n_poses, 6)[:, 0:3]
            prior_res = [(w_ang * dtheta).reshape(-1),
                         (w_pos * (Cs - prior_p)).reshape(-1)]
            if rel_idx:
                # Relative-attitude residual: the rotation the images imply
                # between two poses, against the rotation the gyro measured.
                rel = np.empty((len(rel_idx), 3))
                for m, k in enumerate(rel_idx):
                    predicted = Rs[k - 1] @ rel_dR[m]
                    rel[m] = R_to_axang(predicted.T @ Rs[k])
                prior_res.append((w_rel_ang * rel).reshape(-1))
            return np.concatenate([obs_res] + prior_res)

        # Jacobian sparsity: each observation's two residuals touch only its
        # own camera's 6 parameters and its own point's 3, out of the ~800 in
        # the problem - a bundle-adjustment Jacobian is ~99% zeros. Declaring
        # that is not an optimization detail, it is what makes the solve
        # finish at all: method="lm" wraps MINPACK, which forms the DENSE
        # Jacobian and QR-factorizes it every iteration (O(m*n^2)), and simply
        # hangs at this problem size.
        n_params = len(x0)
        n_res = n_obs_res + 6 * n_poses + n_rel_res
        rows, cols = [], []
        for j, (pk, ptk, _ox, _oy) in enumerate(residual_specs):
            touched = list(range(n_pose_params + 3 * ptk, n_pose_params + 3 * ptk + 3))
            touched += list(range(pk * 6, pk * 6 + 6))
            for r in (2 * j, 2 * j + 1):
                rows.extend([r] * len(touched))
                cols.extend(touched)
        for k in range(n_poses):                       # attitude priors
            for c in range(3):
                rows.append(n_obs_res + 3 * k + c)
                cols.append(k * 6 + c)
        for k in range(n_poses):                       # position priors
            for c in range(3):
                rows.append(n_obs_res + 3 * n_poses + 3 * k + c)
                cols.append(k * 6 + 3 + c)
        base = n_obs_res + 6 * n_poses
        for m, k in enumerate(rel_idx):        # relative-attitude priors
            for c in range(3):
                for pose_k in (k, k - 1):      # touches both poses' rotations
                    for cc in range(3):
                        rows.append(base + 3 * m + c)
                        cols.append(pose_k * 6 + cc)
        jac_sparsity = coo_matrix((np.ones(len(rows)), (rows, cols)),
                                  shape=(n_res, n_params))

        result = least_squares(residuals, x0, method="trf", jac_sparsity=jac_sparsity,
                               tr_solver="lsmr", x_scale="jac", max_nfev=30)
        if not np.all(np.isfinite(result.x)):
            return None

        poses = unpack_poses(result.x)
        moved = max(np.linalg.norm(poses[k][1] - pose0[k][1]) for k in range(n_poses))
        if moved > self.MAX_BA_POSE_JUMP_M:
            return None  # diverged or implausible - keep the incoming pose estimate unrefined

        pts = result.x[n_pose_params:].reshape(-1, 3)
        for local_i, pid in enumerate(point_ids):
            self.points[pid] = pts[local_i]
        for k in range(n_poses):
            self.pose_history[k] = (frame_idxs[k], poses[k][0], poses[k][1], gyro_dR[k])
        return [(frame_idxs[k], poses[k][0], poses[k][1]) for k in range(n_poses)]

    def export_ply(self, filename):
        if len(self.points) == 0:
            return
        pts = np.array(self.points)
        cols = np.array(self.colors)
        with open(filename, "w") as f:
            f.write("ply\nformat ascii 1.0\n")
            f.write(f"element vertex {len(pts)}\n")
            f.write("property float x\nproperty float y\nproperty float z\n")
            f.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
            f.write("end_header\n")
            for (x, y, z), (r, g, b) in zip(pts, cols):
                f.write(f"{x} {y} {z} {int(r)} {int(g)} {int(b)}\n")
