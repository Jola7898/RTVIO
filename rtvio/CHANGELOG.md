# Changelog

## Unreleased

### Removed the EKF/IMU-dead-reckoning trajectory
`inertial_nav_ekf.py` (15-state ESKF: position/velocity/attitude/accel-bias/
gyro-bias, GPS fusion, ZUPT, Mahalanobis-gated vision-pose fusion) and
`trajectory_only.py` (the tool that isolated it for validation) are deleted,
along with their tests (`test_ekf.py`, `test_trajectory_only.py`) and the
`rtvio-trajectory` console script.

Root cause, not a filter bug: every real capture in this repo so far -
`output_flight1`, `output_my_flight`, every `output_indoor_test*` -
measured **zero GPS fixes fused**, the whole session, in every one of them.
With no GPS reaching it, the EKF had no absolute correction of any kind and
ran on pure IMU dead-reckoning, which drifts by construction (uncorrected
accel bias double-integrates into position error growing in a fixed
direction; uncorrected gyro bias curves the heading) - a physically
guaranteed failure mode, not evidence the filter itself was wrong. The EKF's
own test suite (10 checks in the now-removed `test_ekf.py`) passed
throughout.

Replaced with: the camera pose is now `tracking.py`'s `solvePnPRansac`
result directly, every frame it has enough inlier map points (previously
computed and reported but deliberately NOT fed back outside `--indoor` -
see the removed "Vision is deliberately not fused into the filter" section
of README.md, whose own measurements showed fusing it made the EKF's ATE
13-18x worse, because the map PnP resolves against was itself triangulated
from the filter's own earlier poses, so it was never independent
information). Each GPS fix now re-anchors the pose directly (a snap, not a
Kalman blend) instead of correcting a filter, gated by a new
`MAX_GPS_REANCHOR_SIGMA_M` (15 m) threshold on the fix's own accuracy - a
substitute for the down-weighting a Kalman update would have done
automatically. New `gyro_integrator.py`'s `GyroIntegrator` is the one IMU
signal kept: raw, stateless (no bias/covariance) gyro integration between
frames, feeding only `tracking.py`'s windowed-bundle-adjustment relative-
attitude prior - `BA_REL_PRIOR_ANG_RAD`, already documented as the fix for
dense stereo's dominant error source (`Z^2 * dtheta / baseline`). `so3.py`
holds the SO(3) math (`skew`/`axang_to_R`/`R_to_axang`, previously living on
the EKF class) plus a new `level_and_align_attitude` - the EKF's one-shot
gravity-leveling + GPS-course-yaw initialization, extracted as a plain
function since it was never actually a filter operation.

This is a genuine architecture change, not a relabeling: monocular vision
alone has no metric scale, so with GPS sparse or absent there is now nothing
bounding scale/position drift except vision's own internal geometric
consistency (BA's soft priors, `tracking.py`'s `_relative_reinit`) - honest
monocular VO, not VIO. A real tightly-coupled VIO (IMU preintegration
factors + joint sliding-window optimization over pose and landmarks
together, the way MSCKF/VINS-Mono/OKVIS do it) would close that gap, but is
a substantially larger rewrite than this change and remains future work; see
README.md's "Pose comes from vision, not IMU" for the full reasoning. New
`tests/test_pose_pipeline.py` (18 checks) covers what replaced the EKF:
`GyroIntegrator`'s rotation-delta math and stale-gap handling,
`level_and_align_attitude`'s leveling/yaw-alignment correctness, and
`LiveReconstructor`'s GPS-driven init, direct re-anchor, and accuracy-gate
rejection - driven directly against `on_imu`/`on_gps` rather than through a
real socket (`test_stream.py` already covers that plumbing). One of those
tests initially asserted the wrong `gps_seen` count and failed on first run
(the buffered-fixes replay at init also increments it, which the test had
not accounted for) - fixed in the test, not the code, once the actual
counter behavior was confirmed correct by inspection.

### Fixed: `_relative_reinit` gated on processed-frame count, not real time
A congested stretch (frame gaps, late-dropped events - present in every
session so far) can make N processed frames span far more real time than
`REINIT_STRIDE` assumed, long enough that the EKF position delta used for
scale is no longer trustworthy. Measured consequence on a real session:
reconstruction was fine (bounded, ~5m) through t=34s, then diverged to
361m by t=54s - consistent with a bad-scale reinit during a congested
stretch feeding a wrong measurement into a later vision-pose correction.
Replaced `REINIT_STRIDE` (frame count) with `REINIT_MIN_WINDOW_S`/
`REINIT_MAX_WINDOW_S` (real elapsed time, off the wire, not wall-clock
processing time) - a window that took too long is discarded (snapshot
refreshed, nothing reconstructed) rather than trusted. `Tracker.process_frame`
gained a `t_s` parameter to make this possible; `_relative_reinit` returns 0
immediately if not provided rather than falling back to the frame-count
version that produced the measured failure. New regression test
(`test_relative_reinit_ignores_a_too_stale_window`) pins this down.

### Confirmed on real hardware: sparse map finally populates
First real indoor capture with the `_relative_reinit` fix
(`output_indoor_test8`): sparse map went from 0 (every session before this)
to 394 points / 46,151 re-observations, and vision-pose fusion fired 100
times (was 0 in every prior session - it had nothing to correct against
before). The staleness fix above addresses the divergence found by
inspecting that same session's exported trajectory afterward.

### Fixed: sparse map stuck at 0 points on every indoor capture
`tracking.py`'s `Tracker` class has its OWN copy of the aerial-scale
baseline/depth gate (`MIN_BASELINE_M = 4.0`, `MIN/MAX_DEPTH_M = 5-250`),
separate from and upstream of `dense_stereo.py`'s (fixed for `--indoor`
earlier in this changelog) - a track has to pass Tracker's gate before it
can ever reach dense stereo's. This was missed in that earlier fix.
Confirmed via new per-frame diagnostics (`active`/`new`/`reobs` counts,
added below) on a real indoor capture: active tracks stayed healthy
(500-1200 the whole session) while the sparse map stayed at exactly 0,
because no indoor track ever accumulates 4 metres of baseline before
losing view of the feature - it's essentially impossible in a room.
Cross-checked against `../rtvio/LiveVisualInertialMapper.m` (the original
MATLAB implementation `tracking.py` replaced): its `MIN_BASELINE = 0.02`
(2 cm, tuned for a tabletop mug) and `MAX_REPROJ_DEPTH = 20` with no
minimum depth at all confirm this is a real, scale-dependent parameter,
not a hardcoded law - `tracking.py`'s own docstring explains it raised
this specifically to fix aerial-UAV triangulation (two consecutive frames
at flight speed are only ~0.33 m apart), and nobody revisited it for
indoor use afterward. `live_pipeline._lock_intrinsics` now sets
`tracker.MIN_BASELINE_M`/`MIN_DEPTH_M`/`MAX_DEPTH_M` from the same
resolved values `_resolve_stereo_geometry` already computes for
`dense_stereo.py`, so both gates can't disagree again.

### Added per-frame tracker diagnostics
`tracking.py`'s `process_frame` already computed `active_tracks`,
`num_new_points`, and `num_reobserved` every frame; they were computed and
discarded. Now surfaced in the live progress line and `REPORT.md`'s Model
section - this is what made the bug above provable instead of another
guess: `active` heathy + `new`/`map` stuck at 0 pinpoints a promotion-gate
problem specifically, as opposed to a feature-detection or track-survival
problem, which would look different in these same three numbers.

### `--indoor` now uses room-scale stereo geometry
`dense_stereo.py`'s `MIN/MAX_STEREO_BASELINE_M` (4-20 m) and
`MIN/MAX_DEPTH_M` (5-250 m) are tuned for an 80 m-altitude aerial survey.
Applied to handheld indoor capture at 1-3 m range: almost no frame pair
ever reached the 4 m minimum baseline while still overlapping in view
(explaining the persistently tiny keyframe counts across every indoor
session so far), and the depth-range fallback used whenever the sparse map
is too thin searched 5-250 m for a scene actually a few metres away -
plausibly the source of the wedge/fan-shaped point clusters seen in every
indoor capture. `--indoor` now sets these to 0.3-2.5 m baseline and 0.3-8 m
depth (room-scale engineering estimates, not measured); new
`--min/max-stereo-baseline-m` and `--min/max-depth-m` flags override
either individually regardless of `--indoor`. Implemented by having
`live_pipeline.main()` set `dense_stereo`'s module-level constants at
startup (every function that reads them does so as a global at call time,
not a bound default argument, so this needed no changes inside
`dense_stereo.py` itself) - see `_resolve_stereo_geometry`.

### Added `--indoor` mode for GPS-less sessions
New `live_pipeline.py --indoor` flag: skips waiting `COURSE_TIMEOUT_S` for a
GPS-derived heading (starts almost immediately with unobserved yaw instead),
and switches `dense_stereo.py`/`tracking.py`'s baseline/depth gates to a
room-scale preset (see the entry below). At the time this landed, the pose
mechanism itself also differed by `--indoor` (whether `solvePnPRansac`'s
result got fused into the EKF); that distinction no longer exists - see
"Removed the EKF/IMU-dead-reckoning trajectory" above, vision now drives the
pose unconditionally. Not validated against ground truth (none is available
for indoor motion in this repo) - see README.md's "Known limitations" for
what `--indoor` still trades away (yaw stays arbitrary; output is not
georeferenced) and how to validate it (pace out a known path).

### Restructured to a standard `src/` package layout
- Moved all pipeline modules into `src/rtvio/` (installable via
  `pip install -e .`); `stream/` is now `src/rtvio/stream/`.
- Converted intra-package imports to relative imports (`from . import ...`).
- Moved `test_geometry.py` into `tests/`; both test files now import
  `rtvio` as a package instead of patching `sys.path`.
- Moved `camera_intrinsics.json`, `calib_frames/`, and `output_flight1/`
  under `data/` (`data/camera_intrinsics.json`, `data/calib_frames/`,
  `data/outputs/`). `live_pipeline.py`'s `--intrinsics`/`--out-root`
  defaults now point there.
- Consolidated `session1.md`…`session7.md` into `docs/dev_notes/`, and
  `STREAMING.md` / `CAMERA_INTRINSICS_INTEGRATION.md` / `INTRINSICS_SUMMARY.md`
  into `docs/`. Root now holds only `README.md`, `CHANGELOG.md`,
  `CONTRIBUTING.md`.
- Added `pyproject.toml` (setuptools `src` layout, `rtvio-live` console
  script) and a `.gitignore` covering `__pycache__/`, build artifacts, and
  `data/outputs/`.
- Removed `ingest.py`'s `Dataset`/`load_video_frames`/`load_json` — the
  batch loader they existed for was already removed; `sharpness_score` and
  `downsample_intrinsics` (the two the live path actually uses) are kept.
- Removed `tests/test_geometry.py`'s `test_gt_mesh_is_in_enu` - permanently
  dead since its fixture (`gt_scene_mesh.obj`) and its dependency
  (`evaluate.py`) were already removed in the prior cleanup pass.

### Earlier cleanup (see `docs/dev_notes/` for the sessions that produced it)
- Removed the synthetic Blender flight dataset, the batch `pipeline.py` and
  `evaluate.py` that consumed it, and the tools that only existed to drive
  or score it (`tools/replay_dataset_as_phone.py`, `tools/score_live_run.py`).
- Removed stale scratch files: old run outputs, debug screenshots, the
  first prototype demo, and hackathon slide decks.
