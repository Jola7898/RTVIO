# RTVIO — georeferenced 3D reconstruction from UAV video + noisy GPS

Takes a single-pass drone flight and produces a georeferenced dense point
cloud, a textured mesh, a DSM raster, and a speed/quality report. Three
ways to reconstruct live in this package — two built on VGGT, one older
independent algorithm (see the root `../README.md` for why) — plus RTVIO
Studio (`studio/`), the browser UI that drives drone and phone capture and
queues reconstructions:

- **VGGT batch** (`vggt_reconstruct.py`, recommended starting point) — a
  recording or video file in, joint multi-view depth + pose from Meta's VGGT
  transformer out, fused into a dense cloud and a true 3D Screened-Poisson
  mesh (overhangs/vertical faces included, not a flat heightmap). No COLMAP
  needed. See "VGGT batch reconstruction" below.
- **Live streaming** (`live_pipeline.py`) — consumes a phone's video/IMU/GPS
  stream as it arrives, no record-then-process step, producing a pose in
  real time and a 2.5D heightmap mesh for situational awareness during a
  flight. The live camera pose is driven entirely by vision
  (`tracking.py`'s `solvePnPRansac` each frame), re-anchored directly to
  each GPS fix — see "Pose comes from vision, not IMU" below for why, and
  `CHANGELOG.md`'s "Removed the EKF/IMU-dead-reckoning trajectory" for the
  numbers that motivated it. A real capture has no ground truth to score
  against — see `docs/STREAMING.md`'s "What was measured" section for
  accuracy numbers, taken against a synthetic flight with known ground
  truth over a real socket.
- **Live VGGT** (`vggt_live.py`) — the VGGT batch algorithm above, fed by a
  live phone stream instead of a finished recording: a window is
  reconstructed the moment enough new frames have arrived, rather than
  waiting for the whole flight to land on disk first. Shares its window
  code with `vggt_reconstruct.py` exactly (same seams, same fusion), so it
  is not a third, different reconstruction quality - it's the first one,
  started earlier. See "Live VGGT reconstruction" below for what that does
  and does not save you.

Pure `numpy` / `scipy` / `opencv` / `laspy` at the core, plus `torch` (VGGT),
`pymeshlab`/`trimesh` (meshing/export) and `ultralytics` (masking) as
optional extras. No COLMAP, Open3D, GTSAM, pyproj or rasterio — none of them
have a Windows wheel for this machine's Python 3.13, so the pieces they
would normally provide (bundle adjustment, UTM projection, statistical
outlier removal, point-to-mesh distance, plane-sweep stereo) are
implemented here directly.

## VGGT batch reconstruction

```powershell
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo
# with GPS to georeference the result (CSV: timestamp_s,lat_deg,lon_deg,alt_m):
python -m rtvio.vggt_reconstruct --video clip.mp4 --gps track.csv --gps-mode global --out data/outputs/demo
# also write LAS/OBJ/GLB/COLMAP (see table below) alongside the two default files:
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo --extras
# watch it happen: incoming frames + the cloud growing window-by-window + live
# stats (fps, confidence-gate keep %, seam scale/residual, motion blur), then
# explore the finished mesh/cloud in the same tab (see "Live viewer" below):
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo --live-viz --open
# view a finished result later, without --live-viz (needs mesh_poisson.glb,
# which only --extras writes; add --port 8081 if RTVIO Studio holds 8080):
python -m rtvio.view_output data/outputs/demo --open
```

Needs `torch`, the `vggt` + `mesh` extras and the VGGT-1B checkpoint (see
the root README's Installation steps 4-6). With a CPU-only torch, or no
CUDA GPU, `vggt_reconstruct` runs VGGT on the CPU (slow but correct);
without `pymeshlab`/`trimesh` (the `mesh` extra), meshing/GLB export is
skipped with a warning and `cloud_raw.ply` is still written.
`--from-recording DIR` takes a session directory instead of a video file:
an RTVIO Studio take (phone or drone, `data/sessions/<id>/`), a RECORD
LOCALLY session transferred from the app, a `vggt_live` output folder, or
`live_pipeline --record-only` output.

**Lens undistortion.** Applies when the session carries a
`camera_intrinsics.json` with a size and non-zero distortion: a drone take
recorded after its lens was calibrated (see "Drone in RTVIO Studio" below).
- Every frame is remapped to an ideal pinhole camera before VGGT sees it
  (`camera_model.py`). The log says `lens: fisheye, … -> every frame
  undistorted to a pinhole …`.
- `CHECKPOINT_REPORT.md`'s **Camera** section puts VGGT's own focal
  estimate next to the calibrated one.
- `--intrinsics JSON` supplies a calibration for a `--video` file, or for a
  recording without its own.
- `--no-undistort` turns undistortion off.
- `--undistort-balance` (0-1, default 0) trades cropping the lens's outer
  edge (0) against keeping the whole view with black corners (1).
- Phone sessions are unaffected: the Camera2 intrinsics the app sends have
  no size and near-zero distortion.

**What happens** (the full writeup is `vggt_reconstruct.py`'s module
docstring):

1. **Frames** — every frame (`--frame-stride`/`--sample-fps` to subsample).
   Portrait phone frames are rotated to landscape before VGGT sees them
   (its 518px-wide input wastes 43-44% of its tokens/height on a portrait
   frame otherwise); undone on output.
2. **Windows** — frames go through VGGT in overlapping windows sized to
   fill free VRAM (`--window-frames auto`, the default). Bigger windows
   mean more frames solved jointly and fewer window-to-window seams.
3. **Alignment** — each window's own frame/scale is mapped onto the
   previous one by a robust Sim(3) fit over their shared (`--overlap`)
   frames (`fusion.robust_sim3`) — scale is corrected at every seam.
4. **Fusion** — confidence-gated, edge-filtered pixels are averaged into
   voxels about one pixel wide (`fusion.VoxelAccumulator`); voxels seen by
   fewer than `--min-views` frames are dropped, then a statistical outlier
   filter runs. → `cloud_raw.ply`.
5. **Surface** — Screened Poisson on that oriented cloud (`surface.py`),
   trimmed back to faces near real input points (Poisson otherwise invents
   a closed "bubble" surface wherever there's no data), coloured from the
   cloud. → `mesh_poisson.ply`.
6. **Optional (`--extras`)** — LAS, OBJ/GLB, and a COLMAP sparse model (for
   Gaussian-splatting training with gsplat) via `export.py`.

GPS is off unless `--gps-mode global` (implied by passing `--gps`): every
frame with a fix becomes an anchor for one similarity fit of the whole
trajectory. See the root README's "Project status" for the current
georeferencing accuracy gap against the ≤1m target (`test_georeference_vggt.py`
quantifies it against realistic GPS noise).

**Output files** (`data/outputs/<name>/`):

| file | what it is |
| --- | --- |
| `cloud_raw.ply` | dense colored point cloud, binary PLY with per-point normals + view count |
| `mesh_poisson.ply` | Screened Poisson mesh (skipped with a warning if `pymeshlab` isn't installed, or `--no-mesh`) |
| `cameras.json` | per-frame pose + intrinsics in the same output frame as the cloud |
| `CHECKPOINT_REPORT.md` | speed/quality report: frame/window counts, fps, seam scale corrections, motion blur, mesh stats |
| `CHECKPOINT_REPORT.json` | the same report, structured (used by `recon_viz.py`'s live viewer) |
| `trajectory_check.png` | plotted camera trajectory |
| `mesh_poisson.obj` / `.glb` | `--extras` only — same mesh, for viewers/tools that don't read PLY (`view_output.py` needs the `.glb`) |
| `cloud.las` + `mesh_origin.json` | `--extras` only — cloud in a real CRS (UTM), only meaningful with `--gps-mode global` |
| `mesh_2p5d.obj`, `dsm.png` | `--extras` + GPS-mode only — a georeferenced 2.5D heightmap mesh/raster, separate from `mesh_poisson.ply` |
| `colmap/` | `--extras` only — `cameras.txt`/`images.txt`/`points3D.txt` + the exact frames VGGT saw, for gsplat |

`python -m rtvio.view_output <out_dir> --open` serves the folder and opens
a three.js viewer over `mesh_poisson.glb` (`--mesh` to point at a different
`.glb`/`.gltf` — `mesh_2p5d.obj` isn't supported there, glTF/.glb only).

`python -m rtvio.studio [--open]` is a single browser UI (`http://127.0.0.1:8080`)
for everything above, no terminal commands needed after that: it drives
drone capture (below) and phone capture (the app's CONNECT → Start/Stop
recording from the page, and Saved sessions → Transfer), takes a **Video
file** path (any local clip — the "Video file" card, no phone involved) or a
recorded session, queues each on the GPU one at a time (`studio/jobs.py`),
shows a **Watch live** link (the same `--live-viz` viewer described below)
while a job is running, and opens the finished
`cloud_raw.ply`/`mesh_poisson.ply` in its own three.js viewer (Wireframe /
shaded / point size / cameras / Reset view) once done. Ports: web UI 8080
(localhost only unless `--web-host`), phone 5555 (`--phone-port`), live
viewer 8767 (`--recon-viz-port`). Phone takes are reconstructed with the
**Reconstruction settings** card, whose GPS option defaults to **off**; a
drone take follows the drone's own Indoor/Outdoor switch instead.

### Drone in RTVIO Studio

Verified against the real drone on 15 Sep 2026 (indoors, carried by hand:
telemetry, 1280×720 30 fps video, a 184 s take reconstructed automatically);
an Outdoor take with GPS has not been tested yet. The root README's "Field
workflow: drone capture in RTVIO Studio" is the step-by-step guide.

The Studio's left column has a **Drone** tab next to **Phone**. It talks to
the drone directly, the same way the iDronam GCS does (worked out from
iDronam's own bundled code, see `docs/IDRONAM_NOTES.md`), so iDronam does
not need to be running:

- **Video**: `rtsp://<drone IP>:10000/drone_cam` over TCP, decoded by
  OpenCV's bundled FFmpeg and shown live in the tab.
- **Telemetry**: MAVLink v2 over TCP to `<drone IP>:14550` (GCS identity
  255/1). The tab shows flight mode/armed, GPS fix/sats/accuracy,
  position, altitude, speed, attitude and battery.

Set **Drone connection → Drone IP** once (the address iDronam's Add Device
uses; it is saved in `data/studio_settings.json`). **Start recording**
writes the take to `data/sessions/<timestamp>-drone/` in the same layout
as a phone take. **Stop recording** saves it and queues its reconstruction
straight away, whatever the phone's auto-reconstruct checkbox says. The
**Indoor / Outdoor** switch applies to the drone only, and is fixed when a
take starts:

| | Indoor | Outdoor |
|---|---|---|
| GPS recorded | none (`gps_data.json` is empty) | the drone's fused `GLOBAL_POSITION_INT` at 10 Hz, only while it has a 3D fix |
| Reconstruction | `--gps-mode off`: relative, VGGT units | `--gps-mode global`: metric, east-north-up (vision-only if the take got no fixes) |

Every take also keeps all its telemetry in `drone_telemetry.json`. Frames
and GPS are both timestamped by this PC on arrival, so the RTSP latency
makes frames late relative to GPS by a near-constant amount. That is about
1.5 m along track at 5 m/s with 300 ms of latency. Measure it once and set
it as **Video delay (ms)**.

No drone at hand? Run `python tools/mock_drone.py`: a fake MAVLink drone on
port 14550, an armed copter flying a circle. Then set Drone IP to
`127.0.0.1` and Video URL to any local clip, which is looped at its own
frame rate.

**Lens calibration.** The drone's camera is a fisheye: straight lines
visibly bow. VGGT's pinhole model can't represent that, so uncalibrated
takes come out with bent walls and opened-up right angles. How it works:
- **Capture:** Drone tab → **Camera calibration** captures checkerboard
  views from the live video (`studio/camera_calib.py`). A view is kept only
  when the board is somewhere new; 10 are needed, and capture stops at 25.
- **Solve:** `camera_model.py` fits both OpenCV's fisheye (Kannala-Brandt)
  and pinhole (Brown-Conrady) models, and keeps whichever has the lower
  reprojection error. The profile goes to `data/drone_camera.json`.
- **Each take** then writes `camera_intrinsics.json`, the profile scaled to
  its recorded size. `vggt_reconstruct` remaps every frame to an ideal
  pinhole camera before VGGT (see "Lens undistortion" under VGGT batch
  reconstruction above).
- **Older takes:** one recorded before the calibration existed is
  reconstructed with `--intrinsics data/drone_camera.json`, which the
  Studio adds by itself.
- **MAVLink:** the drone is also asked for `CAMERA_INFORMATION` /
  `VIDEO_STREAM_INFORMATION`. Anything it answers is shown in the card and
  saved in the take's `session_meta.json`. Whether the real drone answers
  isn't known yet, because the 15 Sep take predates the request.
- **Tested** on checkerboards rendered through a known ~135° fisheye lens
  (`tests/test_camera_model.py`): fx recovered within 0.1%, field of view
  within 0.1°. Not yet run on the real camera.

**Terminal messages while the drone is connected.** `[drone] drone says: …`
lines are the autopilot's own STATUSTEXT messages (e.g. ArduPilot's
`PreArm: …` checks), relayed as-is. `[rtsp @ …] Illegal temporal ID in
RTP/HEVC packet` lines come from the FFmpeg inside OpenCV and are harmless:
the drone's RTSP server sends one malformed parameter-set packet (VPS/SPS/PPS)
per keyframe, FFmpeg drops it, and the same parameter sets already arrive in
the stream's SDP, so no frame is lost. See the root README's Troubleshooting.

### Live viewer (`--live-viz`)

`--live-viz [--viz-port 8766] [--open]` (`recon_viz.py`) works on both
`vggt_reconstruct.py` and `vggt_live.py` below, for either a plain video
file or a phone recording — the same flag, same page:

```powershell
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo --live-viz --open
python -m rtvio.vggt_reconstruct --from-recording data/sessions/<id> --out data/outputs/demo --live-viz --open
python -m rtvio.vggt_live --out data/outputs/live1 --live-viz --open
```

Opens `http://localhost:8766`: the frame currently being consumed, the
point cloud growing window by window, and a live CAPTURE panel (frames/
windows done, fps, confidence-gate keep %, per-window seam scale/residual,
per-window motion blur). When the run finishes the page fetches the
finished `mesh_poisson.ply`/`cloud_raw.ply` and switches to an explorable
view with Wireframe / Auto-rotate / Reset-view controls and the full
report (same numbers as `CHECKPOINT_REPORT.md`/`.json`). The server stays
up after the run so you can keep exploring — Ctrl-C the process to stop
it. Preview only: a closed/absent browser tab never affects the
reconstruction itself, and leaving `--live-viz` off changes nothing.

## Live VGGT reconstruction

```powershell
python -m rtvio.vggt_live --port 5555 --out data/outputs/live1 --extras
# then point the phone's Settings -> Server IP at this machine and START
# STREAMING (not RECORD LOCALLY - that path has no network for this to
# receive over; use --from-recording on it afterward instead)
# add --live-viz --open for the same live viewer described above - here the
# video panel shows the actual incoming network frames, not a proxy
```

Same flags, same output files, same `_process_window`/`_finalize_and_write`
code as batch `vggt_reconstruct.py` - the only difference is where the
frames come from. GPS georeferencing auto-detects from the stream exactly
like `--from-recording` does: no `--gps-mode` needed unless you want to
force it off.

**What this saves, precisely.** VGGT-1B runs *slower* than real time at
realistic capture rates (measured 4.4-7.0 frames/s on a 16GB RTX 5070 Ti
against a 24-30fps phone or drone stream - see any `CHECKPOINT_REPORT.md`'s
own "Wall time ... (0.10-0.23x real time)" line), so this cannot make
reconstruction finish moments after the flight lands - the unprocessed
backlog still grows for as long as capture continues, at that same ~0.2x
rate. What changes is *when the clock starts*: batch mode's wall time is
`capture_time + vggt_time` because processing cannot begin until the whole
recording exists on disk; live mode's is `max(capture_time, vggt_time)`
plus one window's tail, because the two now overlap. That is a real saving
whenever `vggt_time` is the larger of the two (usually true for anything
past a very short clip), bounded by the *shorter* stage - never by making
VGGT itself faster. Verified by replaying an already-reconstructed
session's frames back through this receiver over a real socket and diffing
the resulting `CHECKPOINT_REPORT.md` against the original batch run: seam
scales and the georeferencing fit matched to within normal GPU run-to-run
floating-point variance (~0.02%).

**What's different from batch mode, and one of them is not minor.** The
final (possibly undersized) window is whatever is left when the stream
disconnects rather than batch's tail-absorption heuristic - genuinely
minor. But window processing also runs synchronously on the
packet-reading thread, so a live view's video queue drops frames during
each ~8-15s VGGT pass exactly as it would against any other slow receiver
(IMU/GPS still get through, only video is dropped) - and on a real 101s
outdoor run this cost far more than expected: only **579 frames arrived
(~5.7 fps)**, against the ~25 fps a RECORD LOCALLY session on the same
phone gets. That is roughly 4-5 out of every 5 frames captured during a
window being dropped, continuously through the whole session, not
concentrated at the end. The reconstruction still completed correctly
(11 windows, one fallback seam, real GPS georeferencing) - it just did so
from a noticeably sparser set of views than the same flight would give
`--from-recording`. Prefer RECORD LOCALLY when reconstruction density
matters more than having a finished model the moment the flight lands.
The full tradeoff writeup is `vggt_live.py`'s module docstring.

**The receiver survives being probed.** The Android app pings Settings ->
Server IP every few seconds with a bare connect-and-disconnect to decide
whether to offer STREAM (see `net/ReceiverProbe.kt`). `main()` loops and
treats any connection that never carried at least 2 frames - including
one that makes the underlying `StreamSession` raise `SystemExit` for
carrying zero packets of either kind - as a probe, not a real stream, and
goes back to listening rather than exiting. Start it before you connect;
it will sit and wait through as many probes as it takes.

## Project layout

```
rtvio/
├── pyproject.toml         package metadata; `pip install -e .` makes `rtvio` importable
├── src/rtvio/              the package — every stage of both pipelines
│   ├── vggt_reconstruct.py VGGT batch entry point (see above) — frames -> windows -> fusion -> mesh
│   ├── vggt_live.py        live VGGT entry point — same window/fusion code, fed by a live phone stream
│   ├── fusion.py           per-window Sim(3) alignment + voxel accumulation for the batch path
│   ├── surface.py          Screened Poisson meshing + trim/component-filter, PLY read/write
│   ├── view_output.py      static three.js viewer over a finished mesh_poisson.glb
│   ├── recon_viz.py        live viewer (--live-viz): incoming frames + growing cloud + stats, batch or live
│   ├── camera_model.py     lens profiles (fisheye/pinhole), checkerboard calibration, undistortion to a pinhole
│   ├── studio/             RTVIO Studio (`python -m rtvio.studio`): browser UI + GPU job queue + capture
│   │   ├── server.py       HTTP API + settings; web/ is the page (plain HTML/JS, vendored three.js)
│   │   ├── jobs.py         one reconstruction subprocess at a time + nvidia-smi GPU monitor
│   │   ├── phone_link.py   phone socket (:5555): remote START/STOP, session files, Transfer receiver
│   │   ├── drone_link.py   drone RTSP video + MAVLink telemetry, drone takes
│   │   ├── mavlink.py      MAVLink v2 codec (no pymavlink needed)
│   │   └── camera_calib.py drone lens calibration: checkerboard views from the live video (solved by ../camera_model.py)
│   ├── ai_masking.py       YOLO dynamic-object (people/vehicle) masking, both paths
│   ├── live_pipeline.py    live entry point: consumes the phone stream, drives everything below
│   ├── viz_server.py       optional browser-based live viewer (--live-viz)
│   ├── ingest.py           blur scoring, intrinsics downsampling
│   ├── so3.py              SO(3) math + one-shot initial-attitude leveling
│   ├── gyro_integrator.py  gyro-only rotation integration for the BA prior (see below)
│   ├── tracking.py         LK feature tracks, solvePnPRansac pose, triangulation, bundle adjustment
│   ├── stream/             wire protocol, clock sync, lat/lon<->ENU, socket source
│   ├── georeference.py     local ENU -> WGS84 -> UTM
│   ├── dense_stereo.py     multi-view plane-sweep stereo (live path)
│   ├── meshing.py          2.5D DSM grid -> textured mesh (both paths' --extras/live output)
│   └── export.py           LAS / OBJ / DSM raster / COLMAP export
├── third_party/vggt/       Meta's VGGT model code, vendored as a git submodule
├── tests/                  test_geometry.py, test_stream.py, test_relative_reinit.py,
│                           test_pose_pipeline.py (live path); test_fusion.py,
│                           test_vggt_bridge.py, test_georeference_vggt.py (VGGT path);
│                           test_drone_link.py, test_camera_model.py (Studio drone link, lens calibration)
├── tools/                  calibrate_camera.py, capture_calibration_frames.py,
│                           import_frames_as_session.py, mock_drone.py (fake MAVLink drone)
├── data/
│   ├── models/             vggt1b_model.pt (download: root README Installation step 6), yolov8n-seg.pt (auto)
│   ├── camera_intrinsics.json  fallback intrinsics (overridden by the phone's own, if sent)
│   ├── drone_camera.json   drone lens calibration from the Studio (its views: drone_calib/<time>/, gitignored)
│   ├── calib_frames/       checkerboard captures for tools/calibrate_camera.py
│   ├── sessions/           RTVIO Studio takes (phone, drone, transferred) + their recon-N/ outputs (gitignored)
│   ├── video_jobs/         RTVIO Studio "Video file" reconstructions
│   ├── studio_settings.json  RTVIO Studio settings, incl. the drone IP (gitignored)
│   └── outputs/            per-run output directories (gitignored)
└── docs/
    ├── STREAMING.md, INTEGRATION.md, CAMERA_INTRINSICS_INTEGRATION.md, INTRINSICS_SUMMARY.md
    ├── IDRONAM_NOTES.md    how the iDronam GCS reaches the drone (source of the Studio's drone link)
    ├── ARCHITECTURE_REDESIGN.md  historical: the LingBot-Map-era redesign plan, superseded by VGGT
    └── dev_notes/          raw development session transcripts, kept for history
```

## Live streaming path

### Pose comes from vision, not IMU

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

### Running it

The live pipeline consumes a phone's video/IMU/GPS stream as it arrives,
there is no record-then-process batch mode (that's what the VGGT path is,
above). See `docs/STREAMING.md` for the full workflow (starting the
receiver, the three-lane architecture, replay for debugging).

```powershell
cd rtvio
python -m pip install -e .                 # once, so `rtvio` is importable

python tests/test_geometry.py              # 9 regression tests, ~2 seconds
python tests/test_stream.py                # 18 acceptance checks, ~2 seconds
python tests/test_relative_reinit.py       # 6 two-view reinit checks, ~1 second
python tests/test_pose_pipeline.py         # 18 gyro/attitude/GPS-reanchor checks, ~1 second
python tests/test_fusion.py                # 23 window-alignment / voxel-fusion / PLY-writer checks, CPU-only
python tests/test_vggt_bridge.py           # 14 phone-recording -> VGGT bridge checks, CPU-only
python tests/test_drone_link.py            # 28 MAVLink codec + mock-drone record/finalize checks, ~7 seconds
python tests/test_camera_model.py          # 27 lens calibration / undistortion checks on rendered fisheye boards, CPU-only
python tests/test_georeference_vggt.py     # 6 checks incl. the GPS-noise sweep vs the 1m target, CPU-only

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

### Pipeline

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

### Measured results

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

### Known limitations

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

## Known limitations (VGGT batch)

**Georeferencing accuracy.** `test_georeference_vggt.py`'s Monte-Carlo sweep
against realistic consumer-GPS noise shows the default `--gps-mode global`
fit currently lands above the ≤1m accuracy target — a real, quantified gap,
not a bug (see `CHANGELOG.md`).

**Meshing/export needs the `mesh` extra.** `pymeshlab`/`trimesh` aren't core
dependencies (`pyproject.toml`'s `vggt` extra deliberately excludes `torch`
and the `mesh` extra for the same reason — the right wheel is
machine-specific). Skip installing `pip install -e ".[mesh]"` and
`vggt_reconstruct` still runs to completion and writes `cloud_raw.ply`, it
just logs `WARNING: Poisson meshing failed (ModuleNotFoundError...)` and
skips `mesh_poisson.ply`/`--extras`' OBJ/GLB/COLMAP silently past that —
worth grepping the log for `WARNING` on a run that looks incomplete.

**Vertical surfaces a single pass never photographed aren't reconstructed**
— this is a property of any MVS method (VGGT included), not something the
meshing step could fix: no camera ray ever hit that geometry, so there is no
depth data to reconstruct it from. `surface.py`'s Poisson trim step
(`poisson_mesh`'s "Distance trim") makes this an *honest* gap rather than a
misleading one: Poisson's implicit function invents a smooth closed surface
wherever there's no data (a "bubble" over every hole), and the trim
deliberately deletes that invented surface rather than keeping a
wrong-but-plausible-looking guess. A single overhead/oblique pass over a
building is the common case — its walls are seen only at a steep grazing
angle if at all, so expect thin, sparse, or entirely absent wall geometry
between a roof and the ground under it, sometimes read from a mesh viewer as
a "floating roof" even when some of that geometry is actually present, just
low-density and hard to see except from the right angle. There is no
automatic fix for this in the pipeline today (a footprint-detect-and-extrude
heuristic was considered and deliberately not built — too easy to misfire on
non-building elevated clutter like tree canopies); the real fix is capturing
the walls, i.e. an oblique/orbit pass around any structure you need
side geometry for.

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
