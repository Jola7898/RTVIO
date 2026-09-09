# RTVIO — georeferenced 3D reconstruction from UAV video + noisy GPS

Takes a single-pass drone flight, streamed live from a phone (compressed
video, consumer-grade GPS, a biased MEMS IMU), and produces a georeferenced
dense point cloud, a textured 2.5D mesh, a DSM raster, and a speed/quality
report. The live camera pose is driven entirely by vision (`tracking.py`'s
`solvePnPRansac` each frame), re-anchored directly to each GPS fix — see
"Pose comes from vision, not IMU" below for why, and `CHANGELOG.md`'s
"Removed the EKF/IMU-dead-reckoning trajectory" for the numbers that
motivated it. A real capture has no ground truth to score against — see
`docs/STREAMING.md`'s "What was measured" section for accuracy numbers,
taken against a synthetic flight with known ground truth over a real socket.

Pure `numpy` / `scipy` / `opencv` / `laspy`. No COLMAP, Open3D, GTSAM, pyproj
or rasterio — none of them have a Windows wheel for this machine's Python
3.13, so the pieces they would normally provide (bundle adjustment, UTM
projection, statistical outlier removal, point-to-mesh distance, plane-sweep
stereo) are implemented here directly.

## Pose comes from vision, not IMU

There is no IMU-integrated trajectory in this pipeline. The camera pose
(`LiveReconstructor.pose_R`/`pose_p`) is:

- **`solvePnPRansac`'s result, every frame it has enough inlier map points**
  — this IS the live pose, not a correction fed into something else.
- **Carried forward unchanged** on a frame where PnP didn't have enough
  points (typically only early in a session, before the sparse map has
  grown) — there is no filter to predict across the gap, so it doesn't
  pretend to.
- **Snapped directly to each GPS fix's ENU position** on arrival — a
  discontinuous re-anchor, not a Kalman blend. A fix whose accuracy is
  worse than `MAX_GPS_REANCHOR_SIGMA_M` (15 m) is rejected outright rather
  than applied, since there is no principled way to merely down-weight it
  without a filter.

`gyro_integrator.py`'s `GyroIntegrator` is the one place raw IMU data still
matters: it integrates gyro samples between frames into a rotation delta
purely for `tracking.py`'s windowed bundle adjustment, whose gyro
relative-attitude prior is the documented fix for dense stereo's dominant
error source (see "Known limitations" below). It never touches the pose
itself.

One direct consequence worth internalizing: with GPS sparse or absent, there
is nothing bounding scale/position drift except vision's own geometry (BA's
soft priors, `tracking.py`'s `_relative_reinit`). This is honest monocular
VO, not VIO — see `CHANGELOG.md` for why a from-scratch tightly-coupled VIO
(IMU preintegration + joint sliding-window optimization) is the real fix for
that, and why it wasn't what got built here.

## Project layout

```
rtvio/
├── pyproject.toml        package metadata; `pip install -e .` makes `rtvio` importable
├── src/rtvio/             the package — every stage of the pipeline
│   ├── live_pipeline.py   entry point: consumes the phone stream, drives everything below
│   ├── viz_server.py      optional browser-based live viewer (--live-viz)
│   ├── ingest.py          blur scoring, intrinsics downsampling
│   ├── so3.py             SO(3) math + one-shot initial-attitude leveling
│   ├── gyro_integrator.py gyro-only rotation integration for the BA prior (see above)
│   ├── tracking.py        LK feature tracks, solvePnPRansac pose, triangulation, bundle adjustment
│   ├── stream/            wire protocol, clock sync, lat/lon<->ENU, socket source
│   ├── georeference.py    local ENU -> WGS84 -> UTM
│   ├── dense_stereo.py    multi-view plane-sweep stereo
│   ├── meshing.py         2.5D DSM grid -> textured mesh
│   └── export.py          LAS / OBJ / DSM raster export
├── tests/                 test_geometry.py (camera-convention regressions),
│                          test_stream.py (live-ingest acceptance checks),
│                          test_relative_reinit.py, test_pose_pipeline.py
├── tools/                 calibrate_camera.py, capture_calibration_frames.py
├── data/
│   ├── camera_intrinsics.json  fallback intrinsics (overridden by the phone's own, if sent)
│   ├── calib_frames/      checkerboard captures for tools/calibrate_camera.py
│   └── outputs/           per-run output_<run-id>/ directories (gitignored)
└── docs/
    ├── STREAMING.md, CAMERA_INTRINSICS_INTEGRATION.md, INTRINSICS_SUMMARY.md
    └── dev_notes/         raw development session transcripts, kept for history
```

## Running it

The pipeline is live-only: it consumes a phone's video/IMU/GPS stream as it
arrives, there is no record-then-process batch mode. See `docs/STREAMING.md`
for the full workflow (starting the receiver, the three-lane architecture,
replay for debugging).

```powershell
cd C:\Users\HP\Desktop\RTVIO\rtvio
python -m pip install -e .                 # once, so `rtvio` is importable

python tests/test_geometry.py              # 9 regression tests, ~2 seconds
python tests/test_stream.py                # 18 acceptance checks, ~2 seconds
python tests/test_relative_reinit.py       # 6 two-view reinit checks, ~1 second
python tests/test_pose_pipeline.py         # 18 gyro/attitude/GPS-reanchor checks, ~1 second

# point the phone app's "Server IP" at this host, then:
python -u -m rtvio.live_pipeline --port 5555 --run-id NAME
# or, equivalently, the installed console script:
rtvio-live --port 5555 --run-id NAME

# no GPS this session (e.g. testing indoors)? add --indoor - see "Known
# limitations" below for exactly what it trades away
python -u -m rtvio.live_pipeline --port 5555 --run-id NAME --indoor --live-viz
```

Use `python -u`. Without it, Python buffers stdout when you redirect to a
file and you see nothing at all until the run ends. `--live-viz` opens a
browser viewer at `http://localhost:8766` (video + IMU + growing point
cloud) — worth having on for an indoor test, since it shows GPS-fix count
live rather than only in the report afterward.

Outputs land in `data/outputs/output_NAME/`:

| file | what it is |
| --- | --- |
| `REPORT.md` | accuracy + speed numbers |
| `cloud.las` + `.prj` | dense cloud in UTM — opens in CloudCompare / QGIS |
| `mesh.obj` / `.mtl` / `_texture.png` | textured 2.5D mesh (local ENU) |
| `mesh_origin.json` | the UTM coordinate the mesh's origin corresponds to |
| `dsm.png` + `.pgw` + `.prj` | georeferenced DSM raster |
| `pose_file.csv` | per-frame georeferenced pose (frozen schema) |
| `sparse_map.ply` | the sparse tracker map |
| `camera_intrinsics.json` | the intrinsics actually used (phone's own, if it sent them; falls back to `data/camera_intrinsics.json`) |

## Pipeline

```
phone stream: JPEG frames + IMU + GPS  (see src/rtvio/stream/protocol.py)
        |
   live_pipeline.py     rolling window, blur flagging (running-median threshold)
        |
   gyro_integrator.py    gyro-only rotation delta, BA prior use only (no pose change)
   tracking.py           solvePnPRansac IS the live pose; LK tracks, multi-view
                         triangulation, bundle adjustment
   on each GPS fix ----> pose snapped directly to it (see "Pose comes from
                         vision, not IMU" above) - no Kalman filter
        |
   stream/geodesy.py    lat/lon/alt -> local ENU (per-fix GPS accuracy)
   georeference.py      local ENU -> WGS84 -> UTM (Snyder, closed form)
        |
   dense_stereo.py      multi-view plane-sweep stereo -> dense colored cloud
        |
   meshing.py           2.5D DSM grid -> textured mesh
   export.py            LAS / OBJ / DSM raster + world file + WKT
```

`ingest.py` supplies two shared utilities (`sharpness_score`,
`downsample_intrinsics`) used by the live path. It used to also define a
batch `Dataset` loader for a now-removed batch pipeline; that loader had no
caller left in this repo and was deleted along with it rather than kept as
dead weight.

## Measured results

**These numbers are stale as of the pose-architecture change** (see
`CHANGELOG.md` "Removed the EKF/IMU-dead-reckoning trajectory") and describe
the REMOVED EKF-driven pose stage, kept here as the historical record of why
that architecture was replaced — they are not a claim about the current
vision-driven pose. Re-measuring end-to-end trajectory/cloud accuracy under
the new architecture (against the same synthetic ground truth these came
from, which has since been removed from this repo — see `docs/dev_notes/`)
is open work.

**Trajectory (EKF-driven pose, since removed)**

| metric | value |
| --- | --- |
| ATE RMSE | **1.29 m** (GPS noise is 2.0 m/axis) |
| absolute attitude error | **4.0°** mean |
| initial attitude error | 2.58° |
| RPE @20 frames | 1.22 m translation, 1.99° rotation |

**Dense stereo, isolated from pose error** (driven by ground-truth poses —
this measures the reconstruction itself, independent of what estimates the
pose; `dense_stereo.py` is unchanged by the pose-architecture change, so
this one is not stale)

| metric | value |
| --- | --- |
| point-to-surface median | **0.29 m** |
| RMSE | 0.84 m |
| within 1 m | 96.5% |
| points | ~344k after filtering, from 16 keyframes |

**End-to-end (EKF-driven pose, since removed)**: ~1.0M points, cloud-to-surface
median ~13 m — see *Known limitations*, this gap was understood and
quantified, not mysterious, but is specific to the pose stage that produced
it and needs re-measuring against the current one.

## Known limitations

**The dense cloud is limited by relative attitude error, not by the stereo**
— true independent of which pose stage produces the poses, since it is a
property of the plane-sweep geometry itself. Plane-sweep depth error from a
relative attitude error `dtheta` is `Z^2 * dtheta / baseline`. At `Z` = 85 m
over a 15 m baseline, the (EKF-era) measured 1.99° of relative attitude
error was ~16 m of depth error — which is what the (EKF-era) end-to-end
cloud actually showed. Two independent checks confirmed the diagnosis rather
than assuming it:

- driving the same stereo code with ground-truth poses gives 0.29 m, so the
  stereo geometry is not the problem;
- lengthening the stereo baseline does *not* help (20.0 m error at a 4–20 m
  baseline, 19.5 m at 35–80 m), because relative attitude error grows with
  the interval and cancels the `1/baseline`. A translation-driven error would
  have fallen off as `1/baseline`.

This is exactly why `GyroIntegrator`'s rotation delta was kept as the one
IMU signal that survived the EKF's removal (see "Pose comes from vision, not
IMU" above): it feeds `tracking.py`'s `BA_REL_PRIOR_ANG_RAD`, the tightest
prior in the whole bundle adjustment, specifically to hold this error down.
Whether that's now sufficient on its own (with the pose itself coming from
PnP + GPS re-anchor rather than an EKF) is unmeasured — see "Measured
results" above.

**No principled GPS/vision fusion.** A GPS fix overwrites `pose_p` outright
rather than being blended in proportion to its own and the pose's relative
confidence — there is no filter here to do that blending correctly, and a
naive one (e.g. a fixed-weight complementary filter) would just be an
unprincipled EKF substitute wearing a different name. The accuracy gate
(`MAX_GPS_REANCHOR_SIGMA_M`) is the one safeguard: it keeps a single poor fix
from moving the pose by tens of metres, but a fix that passes it can still
be a visible jump. `windowed_bundle_adjustment`'s soft position/attitude
priors (`BA_POSE_PRIOR_POS_M`/`ANG_RAD`) are what keep that jump from
whiplashing the *refined* trajectory — the raw `pose_file.csv`/
`trajectory_enu.json` export reflects the jump as-is.

**A PnP miss freezes the pose, it doesn't predict across it.** With no
filter, there is nothing to roll the pose forward on a frame where
`solvePnPRansac` didn't have 6+ inlier map points — REPORT.md's "Vision pose
updates: N / M frames" line and a new warning (below 50%) are what to check
for stretches of the trajectory that are actually stuck, not smoothly
interpolated. This is typically only the first several frames of a session,
before the sparse map has grown past `Tracker.MIN_ACTIVE_TRACKS`'s
neighborhood.

**With little or no GPS, nothing bounds scale/position drift except vision's
own geometry.** This was true of the removed EKF too when GPS was absent
(see `--indoor` below) but is now also true, more quietly, during any
GPS-sparse stretch of an otherwise-outdoor flight: between fixes (or with
none at all), the trajectory is exactly as good as `tracking.py`'s own
consistency (BA's soft priors, `_relative_reinit`'s short-window scale) and
no better.

**`--indoor` mode.** With no GPS expected, `live_pipeline.py` skips waiting
`COURSE_TIMEOUT_S` for a GPS-derived heading (starts almost immediately with
unobserved yaw instead) and switches `dense_stereo.py`/`tracking.py`'s
baseline/depth gates to a room-scale preset — see `INDOOR_MIN/MAX_STEREO_BASELINE_M`.
The pose mechanism itself (`solvePnPRansac` every frame) is now identical
with or without this flag; there is no separate "indoor fuses vision, outdoor
doesn't" behavior any more; the flag only affects startup timing and stereo
geometry. Two things `--indoor` cannot fix, because nothing indoors can: yaw
stays arbitrary (no compass, no true north), and the output is not
georeferenced (`REPORT.md` says so explicitly when no GPS fix was ever seen).

**`tracking.py`'s `Tracker` has its own, separate copy of the aerial-scale
baseline/depth gate** (`MIN_BASELINE_M = 4.0`, `MIN/MAX_DEPTH_M = 5-250`),
and it runs *before* dense stereo ever sees a track - a point cannot reach
dense stereo's pairing stage if this rejects it first. Missed in an earlier
fix; found only once per-frame tracker diagnostics (see below) showed
`active` tracks staying healthy (500-1200) while the sparse map stayed at
exactly 0 for an entire real indoor session - no indoor track can
accumulate 4 m of baseline before losing view of the feature. Cross-checked
against `../rtvio/LiveVisualInertialMapper.m`, the original MATLAB
implementation this module's docstring says it replaced: its baseline gate
was `0.02 m`, tuned for a tabletop mug, with `MAX_REPROJ_DEPTH = 20` and no
minimum depth at all. `tracking.py` raised the bar specifically to fix
aerial triangulation (its own docstring: two consecutive frames at flight
speed are only ~0.33 m apart) and nobody revisited it for indoor use.
`--indoor` sets `Tracker.MIN_BASELINE_M`/`MIN_DEPTH_M`/`MAX_DEPTH_M` from the
exact same resolved values as `dense_stereo.py`'s, rather than introducing a
third, independently-tunable copy of the same numbers.

**Per-frame tracker diagnostics** (`active`/`new`/`reobs` in the live
progress line and `REPORT.md`) surface `tracking.py`'s own
`active_tracks`/`num_new_points`/`num_reobserved`, computed every frame
and previously discarded. This is what made the bug above provable: those
three numbers distinguish "tracking is fine but nothing gets promoted"
(what was actually happening) from "features aren't being found" or
"tracks keep breaking", which would show up differently in the same three
numbers - worth watching on any future session that looks wrong.

**Device-specific notes from real captures on this hardware** (from the
folded-in `HANDOFF.md`): the phone app's own Indoor/Outdoor toggle
(`SettingsManager.kt` in `../rtvioapk`) gates whether `GpsCollector` even
starts (see `MainActivity.kt`'s `settings.outdoorMode` check) — it must be
set to Outdoor for a real flight, or GPS is structurally never going to
arrive regardless of actual sky view. `--keyframe-stride`'s default (8) was
right for this machine; lowering it overloaded the CPU. JPEG quality was
seen as low as 18/100 on the phone in one session — worth raising if dense
stereo quality looks poor for reasons unrelated to pose.

**Other simplifications.** No rolling shutter or wind sway is injected. Yaw
initialization assumes the camera's image-up axis points along the flight
path (`camera_yaw_offset_rad`, default 0). The mesh is 2.5D (one height per
XY cell), so true vertical façades are not represented — standard for
single-pass aerial survey. The very first GPS fix seeds the initial position
without the `MAX_GPS_REANCHOR_SIGMA_M` accuracy gate every later fix gets
(see "No principled GPS/vision fusion" above) — there is no earlier fix to
fall back to yet, so an inaccurate first fix has no substitute, only a
worse one.

## Why `tests/test_geometry.py` exists

Every serious bug this pipeline has had was a **convention mismatch**, not a
wrong formula — a pose in Blender camera axes (`forward = -R[:,2]`) fed into
code assuming OpenCV axes (`forward = +R[:,2]`), or a mesh exported in
Wavefront axes and scored against a trajectory in ENU. None of them crash,
none appear in a stack trace, and one of them produced *three* independent
silent failures at once: the bundle adjustment became a no-op (its
`Xc[2] > 0` guard could never hold, so every residual was zero), the PnP
attitude was 180° out (this was caught back when a rejected PnP measurement
meant the then-EKF's innovation gate discarded it silently; today the same
180°-out attitude would instead corrupt `pose_R` directly, so this class of
bug is if anything more visible now, not less), and the accuracy metric
compared two unrelated coordinate frames.

The tests pin each convention with an assertion that fails loudly:
`CV_FROM_BODY` is a rotation and its own inverse; a point down `-R[:,2]`
lands at the principal point with positive depth; `tracking.py`'s projection
matrix agrees with `dense_stereo.py`'s explicit projection; triangulation
recovers known points; the `solvePnPRansac` round-trip returns the pose it
was given; depth planes are uniform in inverse depth.

Run them before trusting any number this pipeline prints.
