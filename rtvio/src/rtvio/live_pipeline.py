"""
Near-real-time reconstruction from the live phone stream.

    python -m rtvio.live_pipeline --port 5555 --run-id live1
    # or, after `pip install -e .`:
    rtvio-live --port 5555 --run-id live1

THE RULE

The model is built from packets as they arrive. There is no file-reading
code path in this module: `LiveReconstructor` receives decoded packets
from `rtvio.stream.source.StreamSession` and never learns where they came from.
The recorder, if enabled, is a sibling subscriber writing a replayable
fixture; deleting it cannot change the model.

WHAT "NEAR REAL TIME" MEANS HERE, PRECISELY

Three lanes run at three different latencies, because triangulation needs
parallax and parallax needs the camera to have moved:

  ~1 ms     GyroIntegrator folds every IMU sample's gyro reading into a
            rotation delta. No pose change - see "POSE COMES FROM VISION,
            NOT IMU" below - this is purely bookkeeping for the BA prior.
  ~0.3 s    Sparse tracking (solvePnPRansac each frame is the live pose),
            then windowed bundle adjustment over BA_WINDOW frames. Live
            sparse map, refined trajectory.
  ~2-5 s    Dense stereo for keyframe i, fired as soon as frames far
            enough past i exist to give a 4-20 m baseline. Cloud tiles are
            appended to a growing model.

Nothing waits for the session to end. The final mesh/LAS/DSM write at
shutdown is serialisation of a model that was already built, not a second
reconstruction pass.

POSE COMES FROM VISION, NOT IMU

There is no IMU-integrated trajectory here (see CHANGELOG.md "Removed the
EKF/IMU-dead-reckoning trajectory" for the numbers that motivated this): the
live camera pose (self.pose_R/self.pose_p) is tracking.py's solvePnPRansac
result each frame, carried forward unchanged on a frame where PnP has too
few inlier map points, and snapped directly to each incoming GPS fix's ENU
position (no Kalman blending - a discontinuous re-anchor, not a smooth
correction). GyroIntegrator's only job is supplying tracking.py's bundle
adjustment with a gyro-only rotation delta between frames - it never
touches self.pose_R/self.pose_p. One direct consequence: with no GPS at
all, there is nothing to bound scale/position drift except vision's own
geometry (BA's soft priors, _relative_reinit's multi-view triangulation) -
this is honest monocular VO, not VIO.

MEMORY

Only the frames a pending keyframe still needs are held - from the oldest
unprocessed keyframe forward, bounded by SEARCH_AHEAD_FRAMES. That is
~100 frames, ~250 MB at 720x1280. The old batch loader held every frame of
the session, which is ~9 GB for a 60 s 1080p capture and OOMs before
anything else does; the streaming shape removes that failure entirely.

WHAT IS NOT VALIDATED

This module was first exercised end to end against a test double
(tools/replay_dataset_as_phone.py, since removed) that spoke the same wire
format over a real socket but was driven by a synthetic dataset - so
measured numbers from that phase validated the RECEIVER, not the handset.
There has since been at least one real capture from a physical phone (see
data/outputs/output_flight1/REPORT.md), but treat any number not backed by
a real-capture report with the same caution.
"""
import argparse
import csv
import json
import os
import queue
import threading
import time
from collections import deque

import cv2
import numpy as np

from . import dense_stereo
from .dense_stereo import (find_stereo_partners, stereo_views_to_points,
                          depth_range_from_coarse_sweep, depth_range_from_sparse,
                          voxel_downsample, statistical_outlier_removal)
from .export import export_las, write_mesh_origin_sidecar, export_dsm_raster
from .georeference import (georeference_trajectory, course_over_ground,
                          velocity_from_fixes, POSE_FILE_COLUMNS)
from .ingest import sharpness_score, downsample_intrinsics
from .gyro_integrator import GyroIntegrator
from .meshing import build_grid, write_textured_mesh
from .so3 import level_and_align_attitude
from .stream.geodesy import latlon_to_enu, AltitudeSanity
from .tracking import Tracker
from .ai_masking import DynamicMasker

# Attitude initialisation ----------------------------------------------------
# Yaw is the one component neither the accelerometer nor a GPS position fix can
# observe, and leaving it unset is not a small error: measured on the synthetic
# dataset the initial pose starts 72 degrees out and nothing downstream
# recovers it (nothing here observes yaw again once running - see
# level_and_align_attitude). Course over ground from the opening GPS run is a
# yaw observation, so reconstruction cannot start until the platform has
# actually moved. Everything that arrives before then is buffered and
# replayed, not discarded.
COURSE_MIN_FIXES = 6
COURSE_MIN_DISTANCE_M = 5.0
COURSE_TIMEOUT_S = 8.0      # past this, start without a heading and say so
MAX_INIT_BUFFER_FRAMES = 240

# Blur gating ----------------------------------------------------------------
# ingest.py flags a frame when its Laplacian variance is below 0.35 x the
# session median. A live session has no session median, so this is a running
# one over the recent past - which is also more honest for a real capture,
# where illumination changes over the flight.
#
# The 0.35 factor was tuned on synthetic renders. On real handheld footage the
# blur distribution is completely different, and flagging most of the session
# would quietly gut the dense stage - so the flagged fraction is reported at
# shutdown and should be checked before the numbers are believed.
BLUR_MEDIAN_WINDOW = 120
BLUR_FACTOR = 0.35
BLUR_MIN_SAMPLES = 20

# Plausibility gates on the incoming IMU (INTEGRATION.md section 5). The
# synthetic dataset contains samples over 100 g, so "it parsed" is not
# evidence that it is physical.
MAX_PLAUSIBLE_ACCEL = 8.0 * 9.81      # m/s^2
MAX_PLAUSIBLE_GYRO = 35.0             # rad/s, past most MEMS full-scale ranges

# GPS re-anchor quality gate (see on_gps). stream/geodesy.py's gps_sigma_m
# already clamps every fix's accuracy into [MIN_GPS_SIGMA_M, MAX_GPS_SIGMA_M]
# = [1.0, 50.0] m so a Kalman update can't be destabilised by an extreme
# value - but on_gps no longer runs a Kalman update, it snaps self.pose_p
# straight to the fix, which has no equivalent built-in protection. A fix
# this poor is worse than useless to snap to: it would move the pose by up
# to tens of metres on the strength of a single low-confidence reading.
MAX_GPS_REANCHOR_SIGMA_M = 15.0

# Room-scale presets for --indoor. dense_stereo.py's own MIN/MAX_STEREO_BASELINE_M
# (4-20 m) and MIN/MAX_DEPTH_M (5-250 m) are tuned for an 80 m-altitude aerial
# survey (see README.md's "Z^2 * dtheta / baseline" limitation). Applied to a
# handheld indoor capture at 1-3 m range they are wrong by roughly two orders of
# magnitude: almost no indoor frame pair ever reaches a 4 m baseline while still
# overlapping in view, and the depth-range fallback (used whenever the sparse map
# is too thin to constrain it - see depth_range_from_sparse) searches 5-250 m for
# a scene that is actually a few metres away, which cannot land on the right
# answer even by chance. Engineering estimates for room-scale motion, not
# measured: if keyframes are still rare or depths still look wrong, these
# are the first numbers to retune.
INDOOR_MIN_STEREO_BASELINE_M = 0.3
INDOOR_MAX_STEREO_BASELINE_M = 2.5
INDOOR_MIN_DEPTH_M = 0.3
INDOOR_MAX_DEPTH_M = 8.0

# Dense lane queue depth. Bounded on purpose: dense stereo is ~10x slower than
# real time on CPU, so on a long capture it WILL fall behind. The choice is
# between growing a queue until the process dies and shedding keyframes to stay
# current, and for a live system the second is the only honest answer - a
# sparser cloud that tracks the aircraft beats a dense one that stopped
# following it twenty minutes ago. Drops are counted and reported.
DENSE_QUEUE_DEPTH = 4

# --live-viz throttling. The point-cloud/pose pushes above are cheap (a few
# floats); a JPEG frame and 100 Hz IMU are not, and the SSE connection is one
# text stream shared with the cloud - flooding it with every frame/sample
# would starve the point-cloud pushes for no benefit (a human tab can't read
# numbers updating at 100 Hz anyway). These only gate the *preview*; nothing
# else in the pipeline is decimated.
VIZ_FRAME_STRIDE = 3     # ~10 fps preview at a 30 fps capture
VIZ_IMU_STRIDE = 5       # ~20 Hz readout at a 100 Hz IMU



class RollingWindow:
    """The frames a pending keyframe might still need, and nothing else.

    Frames enter, get their depth computed once partners far enough ahead
    exist, contribute points, and are dropped. This is a bounded buffer in
    RAM, not a recording: it never touches disk, its size does not grow
    with session length, and a frame that has passed through cannot be
    revisited.
    """

    def __init__(self, keyframe_stride, search_ahead):
        self.keyframe_stride = keyframe_stride
        self.search_ahead = search_ahead
        self._items = deque()          # dicts, ascending frame_idx
        self.peak_len = 0

    def append(self, item):
        self._items.append(item)
        self.peak_len = max(self.peak_len, len(self._items))

    def __len__(self):
        return len(self._items)

    @property
    def first_idx(self):
        return self._items[0]["idx"] if self._items else None

    @property
    def last_idx(self):
        return self._items[-1]["idx"] if self._items else None

    def slice_from(self, idx):
        """Items with frame index >= idx, oldest first."""
        return [it for it in self._items if it["idx"] >= idx]

    def evict_before(self, idx):
        while self._items and self._items[0]["idx"] < idx:
            self._items.popleft()


class LiveReconstructor:
    """Subscriber that turns the packet stream into a growing 3D model."""

    def __init__(self, out_dir, intrinsics, args):
        self.out_dir = out_dir
        self.args = args
        os.makedirs(out_dir, exist_ok=True)

        self.raw_intrinsics = dict(intrinsics)
        self.phone_intrinsics = None    # set by on_intrinsics callback from phone
        self.K = None                  # set once the first frame confirms the size
        self.frame_wh = None
        self.scale = 1.0
        # Undistort maps, built once at lock time from optional k1/k2/p1/p2/k3
        # fields (tools/calibrate_camera.py writes them; the schema tolerates
        # their absence - see INTEGRATION.md section 4.5). Applied at native
        # resolution, before any downsampling, so they match the calibration.
        self._undistort_map1 = None
        self._undistort_map2 = None

        # Camera pose: driven entirely by vision (tracker.process_frame's
        # solvePnPRansac result each frame) and re-anchored directly to each
        # GPS fix - see the module docstring's "POSE COMES FROM VISION, NOT
        # IMU" and CHANGELOG.md "Removed the EKF/IMU-dead-reckoning
        # trajectory". No position/velocity dead-reckoning, no bias
        # estimation, no covariance - both set for real by _start_reconstruction
        # once initial attitude/position are known.
        self.pose_R = np.eye(3)
        self.pose_p = np.zeros(3)
        # Gyro-only rotation integration between frames, for
        # tracking.py's bundle-adjustment prior alone - see
        # gyro_integrator.py's module docstring for why this one signal is
        # kept when everything else IMU-derived was removed.
        self._gyro = GyroIntegrator()
        self.tracker = None
        self.clock = None

        # Reference origin: the first valid fix, frozen. Everything downstream -
        # georeference_trajectory, export_las - must use this same origin or
        # the model is internally fine and in the wrong place.
        self.ref = None
        self.alt_sanity = AltitudeSanity()
        
        self.masker = DynamicMasker()

        self.initialized = False
        self._pending = []             # (kind, args) buffered until yaw is known
        self._gps_enu_history = []
        self._gps_time_history = []
        self._pending_frames = 0
        self._first_event_t = None
        self.init_course_deg = None
        self.init_speed_mps = None
        # --indoor: GPS will never arrive, so waiting the full COURSE_TIMEOUT_S
        # for a heading that cannot come just delays start for no benefit -
        # start almost immediately with unobserved yaw instead.
        self.course_timeout_s = 0.5 if args.indoor else COURSE_TIMEOUT_S

        # Tracker internals, surfaced in _progress instead of staying invisible.
        # tracking.py already computes all three every frame; nothing here is
        # new work, just no longer throwing away the answer to "is feature
        # tracking actually working" and forcing a guess from symptoms two
        # stages downstream (sparse map size, dense cloud noise).
        self._active_tracks = 0
        self.total_new_points = 0
        self.total_reobserved = 0

        # Per-frame records, kept for the final trajectory export only. These
        # hold poses and timestamps, never images.
        self.frame_times = []
        self.frame_poses = []
        self.refined_poses = []

        self.window = RollingWindow(args.keyframe_stride, dense_stereo.SEARCH_AHEAD_FRAMES)
        self.next_keyframe_idx = 0
        self.frame_count = 0
        self.sharpness_hist = deque(maxlen=BLUR_MEDIAN_WINDOW)
        self.n_blurred = 0

        # The dense lane runs on its own thread(s). Keeping it off the socket
        # reader is what makes the latency split real rather than nominal: a
        # plane sweep takes seconds, and running it inline would stall the
        # gyro-integration and tracking lanes behind it and back-pressure
        # the phone's socket.
        self.cloud_lock = threading.Lock()
        self.cloud_pts = []
        self.cloud_cols = []
        self.n_dense_points = 0
        self.keyframes_done = 0
        self.dense_dropped = 0
        self._dense_q = queue.Queue(maxsize=DENSE_QUEUE_DEPTH)
        self._dense_workers = []

        # Optional live viewer - a preview channel only (see viz_server.py's
        # module docstring). None when --live-viz was not passed, so every
        # push site below is a no-op guarded by this.
        self.viz = None
        if args.live_viz:
            from .viz_server import LiveViz
            self.viz = LiveViz(port=args.viz_port)
            self.viz.start()
            print("live viewer: http://localhost:%d" % args.viz_port)
        self._viz_imu_ctr = 0

        self.implausible_imu = 0
        self.gps_seen = 0                    # every fix received, post-init
        self.gps_rejected_low_accuracy = 0   # sigma_m > MAX_GPS_REANCHOR_SIGMA_M
        self.gps_used = 0                    # fixes that actually re-anchored the pose
        self.gps_rejected_no_ref = 0
        self.vision_pose_used = 0      # frames where PnP had enough inliers to set the pose
        self.lane_time = {"gyro": 0.0, "decode": 0.0, "blur": 0.0,
                          "track": 0.0, "ba": 0.0, "dense": 0.0}
        self.dense_latency_s = []
        self.t_started = None

    # ------------------------------------------------------- session start --

    def on_session_start(self, clock, _hint):
        self.clock = clock
        self.t_started = time.monotonic()
        print("session clock aligned; reconstruction armed")

    def on_intrinsics(self, intrinsics_pkt):
        """Receive camera intrinsics from the phone. These are ground truth and
        should be preferred over any cached or synthetic defaults."""
        self.phone_intrinsics = {
            "K": [[intrinsics_pkt.fx_pix, 0, intrinsics_pkt.cx_pix],
                  [0, intrinsics_pkt.fy_pix, intrinsics_pkt.cy_pix],
                  [0, 0, 1]],
            "distortion": [intrinsics_pkt.k1, intrinsics_pkt.k2,
                          intrinsics_pkt.p1, intrinsics_pkt.p2,
                          intrinsics_pkt.k3],
            "source": intrinsics_pkt.source
        }
        print(f"received camera intrinsics from phone: {intrinsics_pkt.source}")

    # ------------------------------------------------------------ sensors --

    def on_imu(self, t_s, sample):
        if not self._plausible(sample):
            return
        if self.viz is not None:
            # Raw sensor feed, pushed regardless of init state - seeing this
            # update is how you tell "the phone isn't sending IMU" from
            # "reconstruction is still buffered waiting for a heading".
            self._viz_imu_ctr += 1
            if self._viz_imu_ctr % VIZ_IMU_STRIDE == 0:
                self.viz.push_imu(sample.accel, sample.gyro)
        if not self.initialized:
            self._buffer("imu", (t_s, sample))
            return
        t0 = time.perf_counter()
        self._gyro.add_sample(sample.gyro, t_s)
        self.lane_time["gyro"] += time.perf_counter() - t0

    def _plausible(self, sample):
        a = np.linalg.norm(sample.accel)
        g = np.linalg.norm(sample.gyro)
        if a > MAX_PLAUSIBLE_ACCEL or g > MAX_PLAUSIBLE_GYRO or not np.isfinite(a + g):
            self.implausible_imu += 1
            return False
        return True

    def on_gps(self, t_s, pkt, sigma_m):
        if self.viz is not None:
            self.viz.push_gps()
        self.alt_sanity.observe(pkt.altitude_m)
        if self.ref is None:
            self.ref = (pkt.lat_deg, pkt.lon_deg, pkt.altitude_m)
            print("reference origin fixed at first GPS: %.7f, %.7f, %.1f m"
                  % self.ref)
        enu = latlon_to_enu(pkt.lat_deg, pkt.lon_deg, pkt.altitude_m, *self.ref)
        self._gps_enu_history.append(enu)
        self._gps_time_history.append(t_s)

        if not self.initialized:
            self._buffer("gps", (t_s, enu, sigma_m))
            self._try_initialize(t_s)
            return
        # Direct re-anchor, not a Kalman fusion: no principled way here to
        # blend this against vision's own position estimate (see module
        # docstring's "POSE COMES FROM VISION, NOT IMU"), so a GPS fix
        # simply overwrites self.pose_p outright - which is also exactly why
        # sigma_m (stream/geodesy.py's per-fix accuracy, clamped to
        # [MIN_GPS_SIGMA_M, MAX_GPS_SIGMA_M]) still matters here: a Kalman
        # filter would have down-weighted a poor fix automatically, but a
        # direct snap has no such protection, so a fix past
        # MAX_GPS_REANCHOR_SIGMA_M is counted (gps_seen) but not applied
        # (gps_used stays the "actually moved the pose" count report.md's
        # data-health section shows). An applied re-anchor is still a
        # discontinuous jump - _process_frame's pose snapshot for this frame
        # reflects it as-is, and windowed_bundle_adjustment's soft position
        # prior (tracking.py's BA_POSE_PRIOR_POS_M) is what keeps it from
        # whiplashing the refined trajectory.
        self.gps_seen += 1
        if sigma_m > MAX_GPS_REANCHOR_SIGMA_M:
            self.gps_rejected_low_accuracy += 1
            return
        self.pose_p = np.array(enu, dtype=np.float64)
        self.gps_used += 1

    # ------------------------------------------------------- initialisation --

    def _buffer(self, kind, payload):
        if self._first_event_t is None:
            self._first_event_t = payload[0]
        self._pending.append((kind, payload))

    def _estimate_course_and_velocity(self):
        """Initial heading and velocity from the GPS fixes received so far.

        Prefers a least-squares line fit (velocity_from_fixes) over the
        two-point difference course_over_ground uses. Both consume the same
        fixes, but the two-point version throws away every fix in between:
        with metre-scale position noise over a short baseline, the resulting
        heading is dominated by that noise. On this dataset the two-point
        course is 36 deg out after 6 fixes where the truth is ~0 deg, and the
        line fit over the same 6 fixes is far closer.

        The velocity is reported (init_speed_mps) purely for visibility at
        startup - there is no velocity state to seed here (see module
        docstring's "POSE COMES FROM VISION, NOT IMU"), only course, the
        direction component of it.
        """
        n = len(self._gps_enu_history)
        if n < self.args.course_min_fixes:
            return None, None
        v = velocity_from_fixes(self._gps_time_history, self._gps_enu_history)
        if v is not None and np.linalg.norm(v[:2]) * (
                self._gps_time_history[-1] - self._gps_time_history[0]) >= COURSE_MIN_DISTANCE_M:
            return float(np.arctan2(v[1], v[0])), np.asarray(v, dtype=np.float64)
        # Fall back to the two-point course if the fit is unusable (too short a
        # time span for a slope to mean anything).
        return course_over_ground(self._gps_enu_history,
                                  min_distance_m=COURSE_MIN_DISTANCE_M), None

    def _try_initialize(self, t_s):
        """Set the initial pose once the platform has moved enough for
        heading to be observable. Yaw is the one attitude component neither
        the accelerometer nor a GPS position fix can supply, so this
        genuinely cannot happen sooner - and starting without it is not a
        small error."""
        if self.initialized:
            return
        course, velocity = self._estimate_course_and_velocity()
        timed_out = (self._first_event_t is not None
                     and t_s - self._first_event_t > self.course_timeout_s)
        if course is None and not timed_out:
            return
        if course is None:
            print("WARNING: heading is still unobservable after %.1f s - the platform "
                  "has not moved %.0f m. Starting with UNOBSERVED YAW; expect a large "
                  "constant heading error that nothing downstream recovers.%s"
                  % (self.course_timeout_s, COURSE_MIN_DISTANCE_M,
                     " (expected with --indoor: vision (solvePnPRansac) is the only "
                     "drift bound either way, GPS or not, but yaw stays arbitrary)"
                     if self.args.indoor else ""))
        else:
            self.init_course_deg = float(np.degrees(course))
            msg = "initialising: heading %.1f deg from %d fixes" % (
                self.init_course_deg, len(self._gps_enu_history))
            if velocity is not None:
                self.init_speed_mps = float(np.linalg.norm(velocity))
                msg += ", velocity %.1f m/s" % self.init_speed_mps
            print(msg)
        self._start_reconstruction(course)

    def _start_reconstruction(self, course):
        first_accel = next((p[1].accel for k, p in self._pending if k == "imu"), None)
        if first_accel is None:
            return
        self.pose_R = level_and_align_attitude(np.array(first_accel), course_heading_rad=course)
        # Seed position from the first fix, not the origin: an arbitrary
        # start tens of metres from where GPS actually says the platform is
        # would make _relative_reinit's short-window scale (tracking.py)
        # nonsensical for however many frames pass before the first fix
        # after this one.
        self.pose_p = np.array(
            self._gps_enu_history[0] if self._gps_enu_history else [0, 0, 0],
            dtype=np.float64)
        self.initialized = True

        pending, self._pending = self._pending, []
        print("replaying %d buffered events" % len(pending))
        for kind, payload in pending:
            if kind == "imu":
                self._gyro.add_sample(payload[1].gyro, payload[0])
            elif kind == "gps":
                # Same accuracy gate as the live path's on_gps - applied here
                # too so a low-confidence fix that happened to arrive before
                # init can't silently re-anchor the seed position either.
                self.gps_seen += 1
                _t_s, enu, sigma_m = payload
                if sigma_m > MAX_GPS_REANCHOR_SIGMA_M:
                    self.gps_rejected_low_accuracy += 1
                    continue
                self.pose_p = np.array(enu, dtype=np.float64)
                self.gps_used += 1
            elif kind == "frame":
                self._process_frame(payload[0], payload[1])

    # -------------------------------------------------------------- frames --

    def on_frame(self, t_s, pkt):
        if not self.initialized:
            # Budget FRAMES, not total pending events. self._pending also holds
            # IMU at 100+ Hz, so a total-count cap starts discarding frames
            # about a second into the wait - silently, and worse the longer the
            # heading takes to become observable.
            if self._pending_frames < MAX_INIT_BUFFER_FRAMES:
                self._pending_frames += 1
                self._buffer("frame", (t_s, pkt))
            self._try_initialize(t_s)
            return
        self._process_frame(t_s, pkt)

    def _decode(self, pkt):
        img = cv2.imdecode(np.frombuffer(pkt.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if self.K is None:
            self._lock_intrinsics(pkt, img)
        if self._undistort_map1 is not None:
            img = cv2.remap(img, self._undistort_map1, self._undistort_map2, cv2.INTER_LINEAR)
        if self.scale != 1.0:
            img = cv2.resize(img, self.frame_wh, interpolation=cv2.INTER_AREA)
        return img

    def _lock_intrinsics(self, pkt, img):
        h, w = img.shape[:2]
        # Prefer phone_intrinsics (from Camera2 API) over raw_intrinsics (from file).
        # If phone sent intrinsics, they are the ground truth for this device.
        intrinsics = self.phone_intrinsics if self.phone_intrinsics else self.raw_intrinsics

        # Extract width/height. Phone intrinsics don't store width/height explicitly,
        # so infer from K matrix dimensions or use raw_intrinsics as fallback.
        if self.phone_intrinsics and "K" in self.phone_intrinsics:
            # Can't easily infer dimensions from K alone; use the frame size
            # and trust that the phone's calibration is for this resolution
            iw, ih = w, h
        else:
            iw, ih = int(intrinsics["width"]), int(intrinsics["height"])

        if self.phone_intrinsics and "K" in self.phone_intrinsics:
            # Phone sent K directly as a 3x3 matrix
            K_from_phone = self.phone_intrinsics["K"]
            K = np.array(K_from_phone, dtype=np.float64)
            dist = np.array([self.phone_intrinsics.get("distortion", [0, 0, 0, 0, 0])],
                           dtype=np.float64)
            print(f"Using phone camera intrinsics ({self.phone_intrinsics.get('source', 'unknown source')})")
        else:
            # Use raw intrinsics from file
            if (w, h) != (iw, ih):
                raise SystemExit(
                    "camera_intrinsics.json describes %dx%d but the phone is streaming "
                    "%dx%d. These must match exactly - fx/fy/cx/cy are in pixels, so a "
                    "resolution mismatch silently rescales the whole reconstruction. "
                    "Recalibrate at the streaming resolution (INTEGRATION.md section 4.5)."
                    % (iw, ih, w, h))
            K = np.array([[intrinsics["fx"], 0, intrinsics["cx"]],
                          [0, intrinsics["fy"], intrinsics["cy"]],
                          [0, 0, 1]], dtype=np.float64)
            dist_keys = ("k1", "k2", "p1", "p2", "k3")
            dist = None
            if any(k in intrinsics for k in dist_keys):
                dist = np.array([[intrinsics.get(k, 0.0) for k in dist_keys]],
                                 dtype=np.float64)

        if dist is not None:
            # newCameraMatrix=K (not a scaled/cropped one) keeps fx/fy/cx/cy exactly
            # as calibrated - downstream code never has to know the frames were
            # undistorted, only that they now genuinely match a pinhole model.
            self._undistort_map1, self._undistort_map2 = cv2.initUndistortRectifyMap(
                K, dist, None, K, (w, h), cv2.CV_16SC2)
            dist_keys = ("k1", "k2", "p1", "p2", "k3")
            print("undistorting frames with k1=%.5f k2=%.5f p1=%.5f p2=%.5f k3=%.5f"
                  % tuple(dist[0]))
        if self.args.max_long_edge and max(w, h) > self.args.max_long_edge:
            K, (nw, nh), self.scale = downsample_intrinsics(
                K, (w, h), self.args.max_long_edge)
            self.frame_wh = (nw, nh)
            print("downsampling %dx%d -> %dx%d (scale %.3f); intrinsics scaled with it"
                  % (w, h, nw, nh, self.scale))
        else:
            self.frame_wh = (w, h)
        self.K = K
        self.tracker = Tracker(self.K, run_bundle_adjustment=True)
        # Tracker has its OWN copy of the aerial-scale baseline/depth gate
        # (MIN_BASELINE_M=4.0, MIN/MAX_DEPTH_M=5-250), separate from
        # dense_stereo.py's - and it runs first: a track can never reach
        # dense stereo's pairing stage if this rejects it. Missed when
        # dense_stereo's constants were fixed for --indoor. Confirmed on a
        # real indoor capture: active tracks stayed healthy (500-1200) the
        # entire session while the sparse map stayed at exactly 0, because
        # no indoor track ever accumulates 4m of baseline before losing
        # view of the feature. Kept in lock-step with dense_stereo's
        # already-resolved geometry (_resolve_stereo_geometry) rather than
        # introducing a second, independently-tunable copy of the same
        # numbers - one source of truth for "what baseline/depth is valid
        # this session".
        self.tracker.MIN_BASELINE_M = dense_stereo.MIN_STEREO_BASELINE_M
        self.tracker.MIN_DEPTH_M = dense_stereo.MIN_DEPTH_M
        self.tracker.MAX_DEPTH_M = dense_stereo.MAX_DEPTH_M
        self._start_dense_workers()
        cx, cy = K[0, 2], K[1, 2]
        if abs(cx - self.frame_wh[0] / 2) < 1e-6 and abs(cy - self.frame_wh[1] / 2) < 1e-6:
            print("NOTE: principal point is exactly the image centre. A real "
                  "calibrated lens is not centred - this looks like nominal "
                  "intrinsics rather than a calibration.")

    def _process_frame(self, t_s, pkt):
        t_dec = time.perf_counter()
        img = self._decode(pkt)
        if img is None:
            return
        self.lane_time["decode"] += time.perf_counter() - t_dec
        idx = self.frame_count
        self.frame_count += 1

        if self.viz is not None and idx % VIZ_FRAME_STRIDE == 0:
            # pkt.jpeg is the phone's own encode - pushed as-is, no re-encode.
            self.viz.push_frame(pkt.jpeg)

        t0 = time.perf_counter()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharp = sharpness_score(gray)
        self.sharpness_hist.append(sharp)
        blurred = False
        if len(self.sharpness_hist) >= BLUR_MIN_SAMPLES:
            blurred = sharp < BLUR_FACTOR * float(np.median(self.sharpness_hist))
        if blurred:
            self.n_blurred += 1
        self.lane_time["blur"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        track_result = self.tracker.process_frame(
            img, self.pose_R.copy(), self.pose_p.copy(),
            gyro_delta_R=self._gyro.consume(), t_s=t_s)
        self.lane_time["track"] += time.perf_counter() - t0
        self._active_tracks = track_result["active_tracks"]
        self.total_new_points += track_result["num_new_points"]
        self.total_reobserved += track_result["num_reobserved"]

        # This IS the live pose (see module docstring's "POSE COMES FROM
        # VISION, NOT IMU"): solvePnPRansac's result, when tracking.py had
        # enough inlier map points to compute one, replaces self.pose_R/
        # self.pose_p outright - no filter, no blending. On a frame where it
        # didn't (too few map points, typically only early in a session),
        # the pose is carried forward unchanged rather than fabricated from
        # nothing; the next successful GPS fix or PnP result is what moves
        # it again.
        if track_result["pnp"] is not None:
            p_meas, R_meas, _n_inliers = track_result["pnp"]
            self.pose_p = np.asarray(p_meas, dtype=np.float64)
            self.pose_R = np.asarray(R_meas, dtype=np.float64)
            self.vision_pose_used += 1

        pose = (self.pose_R.copy(), self.pose_p.copy())
        self.frame_times.append(t_s)
        self.frame_poses.append(pose)
        self.refined_poses.append(pose)
        if self.viz is not None:
            self.viz.push_pose(pose[1])

        item = {"idx": idx, "t": t_s, "img": img, "blurred": blurred,
                "recv": time.monotonic()}
        self.window.append(item)

        if self.tracker.frame_idx % max(1, self.tracker.BA_WINDOW // 2) == 0:
            t0 = time.perf_counter()
            refined = self.tracker.windowed_bundle_adjustment()
            if refined is not None:
                for f_idx, R_ba, p_ba in refined:
                    j = f_idx - 1          # tracker frame_idx is 1-based
                    if 0 <= j < len(self.refined_poses):
                        self.refined_poses[j] = (R_ba.copy(), np.array(p_ba))
            self.lane_time["ba"] += time.perf_counter() - t0

        self._pump_dense()

        if idx % 30 == 0:
            self._progress(idx, t_s)

    # --------------------------------------------------------- dense lane --

    def _pump_dense(self):
        """Fire every keyframe whose stereo partners have now arrived.

        This is the whole of the "why not instant" answer: keyframe i needs a
        later frame 4-20 m away. Until one exists, i is not computable by any
        implementation. Once one does, i fires immediately and is evicted.
        """
        while self.window.last_idx is not None and self.next_keyframe_idx <= self.window.last_idx:
            k = self.next_keyframe_idx
            items = self.window.slice_from(k)
            if not items or items[0]["idx"] != k:
                # Keyframe already evicted (it was never a keyframe boundary).
                self.next_keyframe_idx += self.window.keyframe_stride
                continue
            poses = [self.refined_poses[it["idx"]] for it in items]
            partners = find_stereo_partners(poses, 0)
            exhausted = (len(items) > dense_stereo.SEARCH_AHEAD_FRAMES
                         or self._baseline_exceeded(poses))
            if not partners and not exhausted:
                return                      # wait for more frames to arrive
            if partners:
                self._enqueue_keyframe(items, poses, partners)
            self.next_keyframe_idx += self.window.keyframe_stride
            self.window.evict_before(self.next_keyframe_idx)

    def _baseline_exceeded(self, poses):
        """True once even the newest frame is too far away to pair with."""
        if len(poses) < 2:
            return False
        d = np.linalg.norm(poses[-1][1] - poses[0][1])
        return d > dense_stereo.MAX_STEREO_BASELINE_M

    def _enqueue_keyframe(self, items, poses, partners):
        """Hand one keyframe to the dense lane. Runs on the reader thread, so
        it must stay cheap - it only selects and packages."""
        ref = items[0]
        if ref["blurred"]:
            return
        usable = [j for j in partners if not items[j]["blurred"]]
        if not usable:
            return
        # The sparse map is snapshotted here rather than read inside the
        # worker: the tracking lane mutates tracker.points on every frame, and
        # reading it from another thread would be a race.
        sparse = (np.array(self.tracker.points, dtype=np.float64)
                  if len(self.tracker.points) else None)
        job = {
            "ref_img": ref["img"],
            "R_ref": poses[0][0], "p_ref": poses[0][1],
            "src_imgs": [items[j]["img"] for j in usable],
            "src_poses": [poses[j] for j in usable],
            "sparse": sparse,
            "recv": ref["recv"],
        }
        try:
            self._dense_q.put_nowait(job)
        except queue.Full:
            # Shed rather than queue. See DENSE_QUEUE_DEPTH.
            self.dense_dropped += 1

    def _start_dense_workers(self):
        for _ in range(max(1, self.args.dense_workers)):
            t = threading.Thread(target=self._dense_loop, daemon=True)
            t.start()
            self._dense_workers.append(t)

    def _dense_loop(self):
        while True:
            job = self._dense_q.get()
            if job is None:
                self._dense_q.task_done()
                return
            try:
                self._reconstruct_keyframe(job)
            except Exception as e:                      # noqa: BLE001
                # One bad keyframe must not take the session down; the model is
                # built incrementally and can lose a tile.
                print("dense keyframe failed: %s: %s" % (type(e).__name__, e))
            finally:
                self._dense_q.task_done()

    def _reconstruct_keyframe(self, job):
        t0 = time.perf_counter()
        R_ref, p_ref = job["R_ref"], job["p_ref"]
        depth_range = depth_range_from_coarse_sweep(
            job["ref_img"], job["src_imgs"], R_ref, p_ref, job["src_poses"], self.K)
        if depth_range is None:
            depth_range = depth_range_from_sparse(
                job["sparse"], R_ref, p_ref, self.K, job["ref_img"].shape)
        pts, cols = stereo_views_to_points(
            job["ref_img"], job["src_imgs"], R_ref, p_ref, job["src_poses"], self.K,
            stride=self.args.stereo_stride, depth_range=depth_range)
        with self.cloud_lock:
            self.keyframes_done += 1
            self.dense_latency_s.append(time.monotonic() - job["recv"])
            self.lane_time["dense"] += time.perf_counter() - t0
            if len(pts):
                self.cloud_pts.append(pts)
                self.cloud_cols.append(cols)
                self.n_dense_points += len(pts)
        if self.viz is not None and len(pts):
            # Outside cloud_lock: push_points does its own decimation/JSON
            # work and must not hold up the next keyframe on this thread.
            self.viz.push_points(pts, cols)

    def _progress(self, idx, t_s):
        with self.cloud_lock:
            lat = (float(np.median(self.dense_latency_s[-20:]))
                   if self.dense_latency_s else float("nan"))
            kf, npts = self.keyframes_done, self.n_dense_points
        print("t=%6.2fs  frame %5d  window %3d  active %4d  new %5d  reobs %6d  "
              "map %5d  keyframes %4d  cloud %8d  dense-lag %5.2fs  q%d"
              % (t_s, idx, len(self.window), self._active_tracks,
                 self.total_new_points, self.total_reobserved,
                 len(self.tracker.points), kf, npts, lat, self._dense_q.qsize()))

    # ------------------------------------------------------------- finish --

    def on_session_end(self, stats):
        print("\n" + stats.summary())
        if not self.initialized or self.K is None:
            print("filter never initialised - nothing to export")
            return
        # Anything still in the window can be reconstructed now: its partners
        # either arrived or never will.
        self._flush_tail()
        self._finalize(stats)

    def _flush_tail(self):
        """Drain the window at shutdown. Every remaining keyframe either has
        its partners already or never will, so this is finishing work that was
        always going to happen - not a second, offline reconstruction pass."""
        while self.window.last_idx is not None and self.next_keyframe_idx <= self.window.last_idx:
            k = self.next_keyframe_idx
            items = self.window.slice_from(k)
            if items and items[0]["idx"] == k:
                poses = [self.refined_poses[it["idx"]] for it in items]
                partners = find_stereo_partners(poses, 0)
                if partners:
                    # Blocking put here, unlike the live path: at shutdown
                    # there is no longer any point shedding load to stay current.
                    ref = items[0]
                    if not ref["blurred"]:
                        usable = [j for j in partners if not items[j]["blurred"]]
                        if usable:
                            sparse = (np.array(self.tracker.points, dtype=np.float64)
                                      if len(self.tracker.points) else None)
                            self._dense_q.put({
                                "ref_img": ref["img"],
                                "R_ref": poses[0][0], "p_ref": poses[0][1],
                                "src_imgs": [items[j]["img"] for j in usable],
                                "src_poses": [poses[j] for j in usable],
                                "sparse": sparse, "recv": ref["recv"],
                            })
            self.next_keyframe_idx += self.window.keyframe_stride
            self.window.evict_before(self.next_keyframe_idx)

        pending = self._dense_q.qsize()
        if pending:
            print("waiting for %d queued keyframes in the dense lane..." % pending)
        self._dense_q.join()
        for _ in self._dense_workers:
            self._dense_q.put(None)
        for t in self._dense_workers:
            t.join(timeout=60)

    def _finalize(self, stats):
        out = self.out_dir
        ref_lat, ref_lon, ref_alt = self.ref if self.ref else (0.0, 0.0, 0.0)

        pts = np.zeros((0, 3))
        cols = np.zeros((0, 3))
        if self.cloud_pts:
            raw = np.vstack(self.cloud_pts)
            rawc = np.vstack(self.cloud_cols)
            pts, cols = voxel_downsample(raw, rawc, voxel_size=0.3)
            pts, cols = statistical_outlier_removal(pts, cols)
            print("dense cloud: %d raw -> %d after filtering" % (len(raw), len(pts)))

        # Real per-frame timestamps, carried from the wire. The batch pipeline
        # derives frame time from frame INDEX (ingest.frame_timestamp), which
        # silently shifts every later frame when the app drops one; the live
        # path never had that mapping to get wrong.
        rows, epsg = georeference_trajectory(
            self.frame_times, [p for _, p in self.frame_poses],
            [R for R, _ in self.frame_poses], ref_lat, ref_lon, ref_alt, self.K)
        with open(os.path.join(out, "pose_file.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=POSE_FILE_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow(r)

        # Local-ENU trajectory, in the same frame self.pose_p was tracked in.
        # pose_file.csv is the frozen deliverable schema and carries UTM;
        # this is the raw estimate, kept so a run can be scored against a
        # ground truth that happens to exist (the synthetic dataset) without
        # a projection round-trip in the way.
        with open(os.path.join(out, "trajectory_enu.json"), "w") as f:
            json.dump([{"timestamp": round(t, 6), "pos_enu": [float(v) for v in p]}
                       for t, (_R, p) in zip(self.frame_times, self.frame_poses)], f)

        # Camera intrinsics from the phone (if received) or from the fallback
        # defaults. The phone's intrinsics override the synthetic defaults.
        intrinsics_to_save = self.phone_intrinsics if self.phone_intrinsics else self.raw_intrinsics
        with open(os.path.join(out, "camera_intrinsics.json"), "w") as f:
            json.dump(intrinsics_to_save, f, indent=2)

        mesh_info = {"n_vertices": 0, "n_faces": 0, "completeness": 0.0}
        grid = None
        epsg_out = None
        if len(pts):
            grid = build_grid(pts, cols, cell_size_m=self.args.cell_size_m)
            mesh_info = write_textured_mesh(grid, os.path.join(out, "mesh.obj"))
            epsg_out = export_las(pts, cols, ref_lat, ref_lon, ref_alt,
                                  os.path.join(out, "cloud.las"))
            write_mesh_origin_sidecar(ref_lat, ref_lon, ref_alt, epsg_out,
                                      os.path.join(out, "mesh_origin.json"))
            export_dsm_raster(grid, ref_lat, ref_lon, ref_alt,
                              os.path.join(out, "dsm.png"))
        self.tracker.export_ply(os.path.join(out, "sparse_map.ply"))

        # The config the batch tools expect, written from what was MEASURED
        # rather than assumed - fps in particular is an observed median, not a
        # setting, and the reference origin is the exact one self.pose_p was
        # tracked against so the two can never drift apart.
        span = (self.frame_times[-1] - self.frame_times[0]) if len(self.frame_times) > 1 else 0.0
        cfg = {
            "fps": round(stats.measured_fps, 3),
            "n_frames": len(self.frame_times),
            "total_time_s": round(span, 3),
            "ref_lat_deg": ref_lat, "ref_lon_deg": ref_lon, "ref_alt_m": ref_alt,
            "gps_noise_std_m": None,
            "note": "gps_noise_std_m is null on purpose: the live path uses the "
                    "per-fix accuracy_m from each packet, not one global constant.",
        }
        with open(os.path.join(out, "session_config.json"), "w") as f:
            json.dump(cfg, f, indent=2)

        self._write_report(out, stats, pts, mesh_info, epsg_out)

    def _write_report(self, out, stats, pts, mesh_info, epsg_out):
        wall = time.monotonic() - self.t_started
        span = (self.frame_times[-1] - self.frame_times[0]) if len(self.frame_times) > 1 else 0.0
        blur_frac = self.n_blurred / max(self.frame_count, 1)
        lat = np.median(self.dense_latency_s) if self.dense_latency_s else float("nan")

        lines = [
            "# RTVIO live reconstruction report", "",
            "Source: live stream (%s)" % ("phone" if not self.args.replay else "replay fixture"),
            "Output: %s" % out, "",
            "## Stream", "", "```", stats.summary(), "```", "",
            "## Latency", "",
            "- Captured %.1f s of stream in %.1f s wall clock (%.2fx real time)"
            % (span, wall, span / max(wall, 1e-9)),
            "- Median dense-lane latency, frame received -> its points exist: %.2f s" % lat,
            "- Lane cost, CPU-seconds: " + ", ".join(
                "%s %.1f" % (k, v) for k, v in self.lane_time.items()),
            "- Per frame: decode %.0f ms, blur %.0f ms, track %.0f ms, BA %.0f ms"
            % tuple(1e3 * self.lane_time[k] / max(self.frame_count, 1)
                    for k in ("decode", "blur", "track", "ba")),
            "- Peak rolling window: %d frames (the old batch loader would have held %d)"
            % (self.window.peak_len, self.frame_count),
            "",
            "## Model", "",
            "- Dense cloud: %d points (EPSG:%s)" % (len(pts), epsg_out),
            "- Sparse map: %d points (%d new features ever detected, %d "
            "re-observations, %d active tracks at end)"
            % (len(self.tracker.points), self.total_new_points,
               self.total_reobserved, self._active_tracks),
            "- Keyframes reconstructed: %d (%d shed to keep the lane current)"
            % (self.keyframes_done, self.dense_dropped),
            "- Mesh: %d vertices, %d faces, %.1f%% completeness"
            % (mesh_info["n_vertices"], mesh_info["n_faces"],
               mesh_info["completeness"] * 100),
            "",
            "## Data health", "",
            "- GPS position re-anchors applied: %d / %d fixes seen (%d rejected, "
            "sigma_m > %.0f m - direct snap, not Kalman-fused, so a poor fix is "
            "dropped rather than merely down-weighted - see live_pipeline.py's "
            "on_gps)" % (self.gps_used, self.gps_seen, self.gps_rejected_low_accuracy,
                        MAX_GPS_REANCHOR_SIGMA_M),
            "- Vision (solvePnPRansac) pose updates: %d / %d frames (this is the "
            "live pose every frame it succeeded on - see module docstring's "
            "'POSE COMES FROM VISION, NOT IMU')" % (self.vision_pose_used, self.frame_count),
        ]
        lines += [
            "- Frames flagged blurred: %d / %d (%.1f%%)"
            % (self.n_blurred, self.frame_count, blur_frac * 100),
            "- IMU samples rejected as implausible: %d" % self.implausible_imu,
            "- Reference origin: %.7f, %.7f, %.2f m"
            % (self.ref if self.ref else (0, 0, 0)),
        ]
        if self.ref is None:
            lines.append(
                "- %s: the reference origin above is a placeholder (0,0,0), not a "
                "real location, and the model below is NOT georeferenced - only "
                "self-consistent." % ("Expected with --indoor" if self.args.indoor
                                      else "NOT expected - check GPS on the phone"))
        elif self.gps_used == 0:
            lines.append(
                "- Reference origin is real, but no GPS fix re-anchored the pose "
                "after startup (%d seen, %d rejected on accuracy) - position after "
                "the initial seed is vision-only (see module docstring's 'POSE "
                "COMES FROM VISION, NOT IMU') and can drift in scale/position with "
                "nothing to pull it back to the real-world coordinates the origin "
                "above claims." % (self.gps_seen, self.gps_rejected_low_accuracy))
        lines += [
            "",
            "## Accuracy", "",
            "No accuracy figure is reported. A live capture has no ground-truth",
            "trajectory and no surveyed checkpoints, so there is nothing to score",
            "against; printing a number here would mean comparing the estimate with",
            "itself. See INTEGRATION.md section 4.3.",
        ]
        vision_frac = self.vision_pose_used / max(self.frame_count, 1)
        if vision_frac < 0.5:
            lines += ["", "> WARNING: solvePnPRansac produced a usable pose on only %.0f%% of "
                          "frames. With no filter to carry a prediction across the gaps, every "
                          "frame it missed kept the PREVIOUS pose exactly - check whether the "
                          "trajectory below has flat/stuck stretches, and whether Sparse map "
                          "above is too small to give PnP enough inlier points."
                          % (vision_frac * 100)]
        if blur_frac > 0.3:
            lines += ["", "> WARNING: %.0f%% of frames were flagged blurred. The 0.35x-median"
                          " threshold was tuned on synthetic renders; on real footage this"
                          " may be gutting the dense stage." % (blur_frac * 100)]
        for w in self.alt_sanity.warnings():
            lines += ["", "> WARNING: %s" % w]
        if stats.late_dropped:
            lines += ["", "> WARNING: %d events arrived too late to order and were dropped."
                          " Raise REORDER_HOLD_S." % stats.late_dropped]

        text = "\n".join(lines)
        with open(os.path.join(out, "REPORT.md"), "w") as f:
            f.write(text + "\n")
        print("\n" + text)
        print("\nAll outputs in: %s" % out)


def build_argparser():
    # This file lives at <repo root>/src/rtvio/live_pipeline.py in an editable
    # install, so data/ is two levels up from the package directory. Not
    # meaningful for a wheel installed elsewhere - this project is only ever
    # run from a source checkout, never distributed as a built package.
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(package_dir))
    data_dir = os.path.join(repo_root, "data")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--run-id", default="live")
    ap.add_argument("--intrinsics", default=os.path.join(data_dir, "camera_intrinsics.json"),
                    help="calibrated at the streaming resolution; width/height must "
                         "match the frames exactly")
    ap.add_argument("--out-root", default=os.path.join(data_dir, "outputs"))
    ap.add_argument("--cell-size-m", type=float, default=1.0)
    ap.add_argument("--stereo-stride", type=int, default=2,
                    help="pixel stride in the plane sweep; the main speed/density knob")
    ap.add_argument("--keyframe-stride", type=int, default=dense_stereo.KEYFRAME_STRIDE)
    ap.add_argument("--course-min-fixes", type=int, default=COURSE_MIN_FIXES,
                    help="GPS fixes to accumulate before the initial heading is "
                         "estimated. This is a direct startup-latency vs "
                         "heading-accuracy trade: more fixes means a longer baseline "
                         "and a better course, but reconstruction stays unstarted "
                         "(and everything is buffered) until they arrive")
    ap.add_argument("--dense-workers", type=int, default=2,
                    help="threads running plane-sweep stereo. The sweep is numpy-"
                         "heavy and releases the GIL, so >1 genuinely parallelises")
    ap.add_argument("--max-long-edge", type=int, default=0,
                    help="downsample frames to this long edge (0 = native); "
                         "intrinsics are scaled to match")
    ap.add_argument("--live-viz", action="store_true",
                    help="serve a browser-based live point-cloud viewer at "
                         "--viz-port. Preview only - see viz_server.py")
    ap.add_argument("--viz-port", type=int, default=8766)
    ap.add_argument("--indoor", action="store_true",
                    help="no GPS is expected this session. Stops waiting for a "
                         "GPS-derived heading (starts almost immediately with "
                         "unobserved yaw instead of waiting COURSE_TIMEOUT_S for "
                         "fixes that will never come) and switches dense stereo/"
                         "the sparse tracker to room-scale baseline/depth presets "
                         "(see INDOOR_MIN/MAX_STEREO_BASELINE_M). The pose itself "
                         "(solvePnPRansac each frame) is identical with or without "
                         "this flag - see module docstring's 'POSE COMES FROM "
                         "VISION, NOT IMU'. Trade-off: yaw stays arbitrary (no "
                         "compass, no true north) and, with no GPS fix ever "
                         "received, the output is NOT georeferenced, only "
                         "self-consistent.")
    ap.add_argument("--min-stereo-baseline-m", type=float, default=None,
                    help="minimum camera-to-camera translation to treat two frames "
                         "as a stereo pair. Default: dense_stereo.py's own %.1f m "
                         "(aerial-survey scale), or %.1f m automatically with "
                         "--indoor (room scale) - see INDOOR_MIN_STEREO_BASELINE_M's "
                         "comment for why these differ by two orders of magnitude"
                         % (dense_stereo.MIN_STEREO_BASELINE_M, INDOOR_MIN_STEREO_BASELINE_M))
    ap.add_argument("--max-stereo-baseline-m", type=float, default=None,
                    help="default: %.1f m normally, %.1f m with --indoor"
                         % (dense_stereo.MAX_STEREO_BASELINE_M, INDOOR_MAX_STEREO_BASELINE_M))
    ap.add_argument("--min-depth-m", type=float, default=None,
                    help="fallback plane-sweep depth range floor, used when the "
                         "sparse map is too thin to constrain it. Default: %.1f m "
                         "normally, %.1f m with --indoor"
                         % (dense_stereo.MIN_DEPTH_M, INDOOR_MIN_DEPTH_M))
    ap.add_argument("--max-depth-m", type=float, default=None,
                    help="default: %.1f m normally, %.1f m with --indoor"
                         % (dense_stereo.MAX_DEPTH_M, INDOOR_MAX_DEPTH_M))
    ap.add_argument("--record", metavar="DIR",
                    help="also write a replayable fixture here. A sibling subscriber; "
                         "it cannot affect the model")
    ap.add_argument("--replay", metavar="DIR",
                    help="drive from a recorded fixture instead of a socket. Debugging "
                         "only - a model built this way is a model built for debugging")
    ap.add_argument("--replay-speed", type=float, default=0.0,
                    help="0 = as fast as possible, 1 = original pace")
    return ap


def _resolve_stereo_geometry(args):
    """Aerial defaults unless --indoor (room-scale preset) or an explicit
    --min/max-stereo-baseline-m / --min/max-depth-m override says otherwise.
    Mutates dense_stereo's module-level constants directly rather than
    threading a parameter through every function that reads them - every
    such function looks these up as globals at call time (not as bound
    default-argument values), so this is equivalent and touches none of
    that already-tested numerical code."""
    overridden = False
    for flag, attr, indoor_val in (
        ("min_stereo_baseline_m", "MIN_STEREO_BASELINE_M", INDOOR_MIN_STEREO_BASELINE_M),
        ("max_stereo_baseline_m", "MAX_STEREO_BASELINE_M", INDOOR_MAX_STEREO_BASELINE_M),
        ("min_depth_m", "MIN_DEPTH_M", INDOOR_MIN_DEPTH_M),
        ("max_depth_m", "MAX_DEPTH_M", INDOOR_MAX_DEPTH_M),
    ):
        explicit = getattr(args, flag)
        if explicit is not None:
            setattr(dense_stereo, attr, explicit)
            overridden = True
        elif args.indoor:
            setattr(dense_stereo, attr, indoor_val)
    print("stereo geometry (sparse tracker's triangulation gate AND dense "
          "stereo's keyframe pairing both use this): "
          "baseline [%.2f, %.2f] m, depth [%.2f, %.2f] m%s"
          % (dense_stereo.MIN_STEREO_BASELINE_M, dense_stereo.MAX_STEREO_BASELINE_M,
             dense_stereo.MIN_DEPTH_M, dense_stereo.MAX_DEPTH_M,
             " (explicit override)" if overridden
             else " (room-scale preset from --indoor)" if args.indoor
             else " (aerial-survey default)"))


def main():
    args = build_argparser().parse_args()
    _resolve_stereo_geometry(args)
    out_dir = os.path.join(args.out_root, "output_%s" % args.run_id)
    intrinsics = json.load(open(args.intrinsics))

    recon = LiveReconstructor(out_dir, intrinsics, args)
    subscribers = [recon]
    if args.record:
        from .stream.recorder import SessionRecorder
        subscribers.append(SessionRecorder(args.record, intrinsics))
        print("recording a fixture to %s (sibling subscriber; cannot affect the model)"
              % args.record)

    if args.replay:
        from .stream.replay import ReplayPacketSource
        source = ReplayPacketSource(args.replay, speed=args.replay_speed)
        print("REPLAY MODE: driving from %s. This is a debugging path - the model "
              "it produces is not a live capture." % args.replay)
    else:
        from .stream.source import SocketPacketSource
        source = SocketPacketSource(args.host, args.port)

    from .stream.source import StreamSession
    StreamSession(source, subscribers).run()


if __name__ == "__main__":
    main()
