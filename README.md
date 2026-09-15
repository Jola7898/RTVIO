# RTVIO

**Georeferenced 3D reconstruction from a single-pass drone/phone flight.**
Video + GPS in → a dense point cloud, textured mesh, and elevation raster out.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20(tested)%20%7C%20Linux%2FmacOS%20(untested)-lightgrey)
![Status](https://img.shields.io/badge/status-hackathon%20prototype-orange)
![License](https://img.shields.io/badge/license-not%20yet%20published-red)

Built against SIH 2026 PS-17 ("Single-Pass Drone Video to Accurate 3D Model
Generation" — `SIH26158.pdf`, pages 37–39): single-pass drone video + GPS
in, ≤1m-accuracy georeferenced model out, in under 15 minutes for a
10-minute video.

## Table of contents

- [What's in here](#whats-in-here)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Usage](#usage)
- [Field workflow: drone capture in RTVIO Studio](#field-workflow-drone-capture-in-rtvio-studio)
- [Field workflow: phone capture → desktop reconstruction](#field-workflow-phone-capture--desktop-reconstruction)
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Project status & known limitations](#project-status--known-limitations)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

## What's in here

Two halves that talk to each other over a WiFi socket, plus a direct drone
link:

- **`rtvio/`** — the Python reconstruction engine. Three ways to run the
  same core algorithms: a **batch VGGT pipeline** (a recording or video
  file in, dense cloud + mesh out — the actively-developed, most-accurate
  path), a **live VGGT pipeline** (`vggt_live.py`) that runs that exact
  same window-by-window algorithm as frames arrive over the network
  instead of waiting for the recording to finish, and an older **live
  streaming pipeline** (`live_pipeline.py`, a different and cruder
  monocular-vision algorithm) that produces a pose in real time for
  "situational awareness" while a flight is in progress. **RTVIO Studio**
  (`python -m rtvio.studio`) puts capture, the GPU job queue and a 3D
  viewer in one browser page.
- **`rtvioapk/`** — an Android app (Kotlin) that captures the phone's
  camera, IMU, and GPS and sends them over WiFi to `rtvio/`, or records
  them to the phone for transfer later. The phone does capture and
  transmission only; all reconstruction happens on the desktop/GPU side.
- **Drone capture** — RTVIO Studio can also record straight from a MAVLink
  drone with an RTSP camera, such as the Suparna drone the iDronam GCS
  flies. It shows live video and telemetry, records takes with or without
  GPS (Indoor/Outdoor), and reconstructs each take as soon as it stops. The
  drone needs no app or extra software.

Two sample video clips at the repo root (real drone/phone footage) let you
try the pipeline immediately without a phone or drone.

## Features

- Georeferenced dense point cloud and a true 3D textured mesh (Screened
  Poisson surface reconstruction — overhangs and vertical faces included,
  not a flat heightmap) from a single drone pass
- Batch or live VGGT reconstruction (joint multi-view depth + pose, no
  COLMAP needed) and a separate real-time monocular vision pipeline with
  GPS re-anchoring (2.5D heightmap mesh + DSM raster — see "The
  reconstruction paths" below for why there are two different algorithms,
  and what each actually outputs)
- Android capture app: record through RTVIO Studio over WiFi (every frame
  kept), record fully offline and transfer later, or stream live — no
  dedicated flight-controller integration required
- Direct drone capture: RTSP video + MAVLink telemetry from the drone
  itself (built against the iDronam/Suparna setup, iDronam not needed), with
  an Indoor/Outdoor switch that decides whether the drone's GPS
  georeferences the result
- Camera intrinsics auto-discovered from the phone's Camera2 API, with a
  manual checkerboard-calibration fallback tool
- Dynamic-object (people/vehicle) masking via YOLO before reconstruction
- Exports to LAS (point cloud), OBJ/GLB (mesh), a georeferenced DSM raster,
  and a COLMAP dataset (for downstream tools like gsplat)
- **RTVIO Studio** — a single browser UI that drives drone capture (live
  RTSP video + MAVLink telemetry, Indoor/Outdoor mode, reconstruct on stop)
  and phone capture, reconstructs any local video file, queues GPU
  reconstructions, and opens the result in a 3D viewer

## Requirements

| | |
|---|---|
| OS | Developed and tested on Windows 10/11. The Python core has no Windows-only code paths, but Linux/macOS are untested — see [Troubleshooting](#troubleshooting). |
| Python | 3.10+. The project is developed and tested on **3.13** (3.13.15); if a wheel is missing for your version, 3.10–3.12 have the widest prebuilt-wheel coverage. |
| Git | with submodule support (any reasonably recent Git) |
| Disk space | ~5 GB for the VGGT-1B checkpoint, plus room for captures: frames are kept as JPEGs (~180 MB per minute of 1280×720 video at 30 fps), and each reconstruction adds a few hundred MB (a ~7-million-point `cloud_raw.ply` is ~200 MB). |
| GPU | Optional but strongly recommended for VGGT — NVIDIA + CUDA. A CPU fallback exists (correct, just very slow). Tested on a 4 GB GTX 1650 and a 16 GB RTX 5070 Ti. **RTX 50-series (Blackwell) cards need a PyTorch build for CUDA 12.8 or newer** (see Installation step 4). |
| Android app (optional) | Android Studio (bundles a suitable JDK + SDK manager), or JDK 17 + Android SDK platform 34 command-line tools. A physical Android 7.0+ (API 24+) phone. |
| Drone (optional) | A MAVLink drone this PC can reach over the network, with telemetry on TCP (default port 14550) and an RTSP camera (default `rtsp://<ip>:10000/drone_cam`), e.g. the Suparna drone iDronam flies. No extra software: `opencv-python` (already a dependency) reads the RTSP stream, and MAVLink decoding is built in. |

**Reference machine** (where everything in this README was last checked, 15
Sep 2026): Windows 11 Pro, Python 3.13.15, PyTorch 2.12.0 nightly for CUDA
12.8 (`2.12.0.dev20260408+cu128`), opencv-python 5.0.0.93, NVIDIA RTX 5070 Ti
16 GB, JDK 17.

## Installation

**1. Clone the `rtvio-python-pipeline` branch, including its submodule.** The
VGGT model code lives in a submodule (a plain clone leaves that folder
empty), and all of the code in this README is on the `rtvio-python-pipeline`
branch: the repository's default branch, `main`, is the original August
version and has none of it.

```bash
git clone --recurse-submodules --branch rtvio-python-pipeline https://github.com/Jola7898/RTVIO.git
cd RTVIO
```

Already cloned? Switch branch and fetch the submodule:

```bash
git checkout rtvio-python-pipeline
git submodule update --init --recursive
```

**2. Create and activate a virtual environment** (recommended, not required):

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (cmd)
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate
```

If PowerShell refuses to run `Activate.ps1`, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**3. Install the core package** (editable install — code changes take effect immediately, no reinstall needed):

```bash
cd rtvio
python -m pip install -e .
```

**4. Install PyTorch** — needed for VGGT. It isn't pinned in `pyproject.toml`
because the right build depends on your GPU and driver; get the exact
command for your machine from
**[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)**.
For an RTX 50-series card (and any recent NVIDIA card), use a CUDA 12.8+
build, e.g.:

```bash
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

CPU-only (works, just much slower for VGGT):

```bash
python -m pip install torch torchvision
```

Check that PyTorch sees the GPU before going further:

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

Do this step **before** step 5: the `masking` extra pulls in `ultralytics`,
which depends on `torch`, and if no torch is installed yet pip fetches the
default (CPU-only on Windows) build from PyPI.

**5. Install the optional extras** (VGGT model loading, meshing/export, YOLO masking):

```bash
python -m pip install -e ".[vggt,mesh,masking]"
```

`trajectory_check.png` needs `matplotlib`, which `ultralytics` brings along;
if you skip the `masking` extra, `pip install matplotlib` or the plot is
skipped with a warning.

**6. Download the VGGT-1B checkpoint** (~5 GB, one time). The pipeline looks
for it at `rtvio/data/models/vggt1b_model.pt`:

```bash
curl -L --create-dirs -o data/models/vggt1b_model.pt https://huggingface.co/facebook/VGGT-1B/resolve/main/model.pt
```

(`curl` ships with Windows 10/11; run it from `rtvio/`.) If you skip this, the
first reconstruction downloads the model through Hugging Face instead and
caches it in the Hugging Face cache (`~/.cache/huggingface`), not in
`data/models/` — it still works, but every run then logs "local checkpoint
not found … falling back to from_pretrained". A checkpoint stored somewhere
else can be used by pointing `RTVIO_VGGT_CHECKPOINT` at the `.pt` file.

**7. Verify the install** — fast, CPU-only, no GPU or checkpoint needed:

```bash
python tests/test_geometry.py
python tests/test_stream.py
```

Both should print an all-pass summary (`9/9 passed`, `18 passed, 0 failed`).
If they do, the core install is good. [Testing](#testing) lists the rest.

## Quickstart

See a real reconstruction with nothing beyond the steps above, using one
of the sample clips at the repo root (201 frames of 4K drone footage):

```bash
cd rtvio
python -m rtvio.vggt_reconstruct --video ../11240137-uhd_3840_2160_25fps.mp4 --out data/outputs/quickstart --extras
python -m rtvio.view_output data/outputs/quickstart --open
```

On the reference machine the first command takes about 1.5 minutes (VGGT
itself: 31 s) and produces a ~600k-point cloud and a ~1.3M-face mesh.
`--extras` matters here: it writes `mesh_poisson.glb`, the file `view_output`
opens; without it only `cloud_raw.ply`/`mesh_poisson.ply` are written. If
RTVIO Studio is already running it holds port 8080, so add `--port 8081` to
`view_output`.

Prefer a UI? `python -m rtvio.studio --open`, paste the clip's full path into
the **Video file** card and press **Reconstruct**; the Studio's own viewer
opens the PLY files directly, no `--extras` needed.

## Usage

This file gets you to a first working run and through the field workflows
below. For everything past that — the live streaming pipeline, calibration
tools, the Android app's internals — see the [Documentation](#documentation)
table, which points at the file that actually covers each one in depth.

## Field workflow: drone capture in RTVIO Studio

For a MAVLink drone with an RTSP camera — built against the Suparna 5G
drone that the iDronam GCS flies — RTVIO Studio records straight from the
drone. No phone, no app, and iDronam doesn't need to be running. How the
connection was worked out is in
[`rtvio/docs/IDRONAM_NOTES.md`](rtvio/docs/IDRONAM_NOTES.md).

> **Status:** verified against the real drone on 15 Sep 2026, indoors:
> telemetry (ArduPilot), 1280×720 30 fps video, and a 184 s take (5,520
> frames, 0 dropped) that was reconstructed automatically. The drone was
> carried by hand, not flown, so an **Outdoor take with GPS has not been
> tested yet**, and the video delay (step 6) has not been measured. See
> [Project status](#project-status--known-limitations).

**1. Put this PC on the drone's network**, the one iDronam uses to reach
the drone. The Studio uses the same two ports on the drone as iDronam:

| | Default | Carries |
|---|---|---|
| MAVLink telemetry | TCP `<drone IP>:14550` | flight mode, GPS, attitude, battery, and the GPS track for Outdoor takes |
| Camera | `rtsp://<drone IP>:10000/drone_cam` | the video that is recorded and reconstructed |

**2. Start the Studio:**

```bash
cd rtvio
python -m rtvio.studio --open        # http://127.0.0.1:8080
```

**3. Enter the drone's IP once.** Open the **Drone** tab, then **Drone
connection → Drone IP** ("Connect to the drone" is ticked by default). Use
the same address as iDronam's "Add Device" screen; if you're not sure, the
PC's default gateway on the drone's network (`ipconfig`) is usually it. It is
saved in `rtvio/data/studio_settings.json`, which is gitignored. Within a few
seconds the tab should show live video, and the telemetry panel should show
the flight mode, GPS fix and battery. The terminal logs `[drone] telemetry
connected`, `[drone] vehicle found: system 1, ArduPilot` and `[drone] video
connected`.

**Lens calibration (once per camera).** The drone's camera has a fisheye
lens: straight walls visibly bow in its frames. VGGT models a pinhole
camera, so it turns that bow into bent walls and opened-up corners. On the
first real take, a 90° wall corner came out near 150°. The drone doesn't
report its lens over MAVLink, so calibrate it once with a printed
checkerboard:

1. Print a checkerboard and note its **inner** corners (a board of 10 × 7
   squares has 9 × 6). Tape it flat to something rigid. The square size
   doesn't matter.
2. In the Drone tab, open **Camera calibration** (it opens by itself while
   the lens is uncalibrated), enter the inner-corner counts, and press
   **Start capturing**.
3. Hold the board in front of the camera and move it slowly: near and far,
   tilted, and into every corner of the view.
   - The live view draws the corners it finds.
   - A view is kept only when the board is somewhere new.
   - The 3 × 3 grid shows which parts of the image are covered. Cover every
     cell, especially the corners, where the lens bends most.
4. Once you have at least 10 views, press **Calibrate**. Capture stops by
   itself at 25.
   - It fits a fisheye and a pinhole lens model and keeps whichever has
     the lower error.
   - The card then shows the lens's field of view and the reprojection
     error. Under about 0.5 px is good.

The result is saved to `rtvio/data/drone_camera.json`, and the views as
JPEGs under `rtvio/data/drone_calib/<time>/`. From then on:
- Every take carries the calibration as `camera_intrinsics.json`, and its
  frames are straightened before VGGT sees them.
- Tick **show undistorted** under the live view to see exactly what VGGT
  gets. The lens's outermost edge is cropped.
- Takes recorded *before* you calibrated are straightened too when you
  press **Reconstruct again**.
- Each reconstruction's `CHECKPOINT_REPORT.md` has a **Camera** section
  comparing VGGT's own focal-length estimate with the calibrated one.

Recalibrate if the camera, its resolution or its zoom changes. Saved views
can also be solved offline:
`python tools/calibrate_camera.py --images <folder of views> --board-cols 9
--board-rows 6 --square-size-mm 25 --model auto --out data/drone_camera.json`.

**4. Pick Indoor or Outdoor** before you start a take. The switch is only
on the Drone tab (phone takes and video files use the Reconstruction
settings' GPS option), and it can't be changed while recording:

| | Indoor | Outdoor |
|---|---|---|
| GPS | not recorded | the drone's GPS track at 10 Hz, only while it has a 3D fix |
| Result | relative model, VGGT units | georeferenced model in metres (east-north-up) |

For Outdoor, wait until the GPS row shows **3D fix** (the tab warns if it
doesn't). A take that ends up with no fixes is reconstructed vision-only.

**5. Start recording → fly the pass → Stop recording.** Stopping saves the
take to `rtvio/data/sessions/<timestamp>-drone/` and queues its
reconstruction on the GPU **straight away**. The take appears in
**Sessions** like a phone take: first progress and a **Watch live** link,
then **View cloud / View mesh** and downloads once it's done. Reconstruction
runs at roughly 7 frames/s on the reference machine, so a 3-minute take
takes about 14 minutes. Flying follows the same parallax rule as filming
with the phone
([step 3 below](#3-how-to-actually-move-the-phone--read-this-before-you-fly)):
fly through or around the subject with lots of overlap. Don't hover and
yaw in place.

**6. For Outdoor accuracy, set the video delay once.** Frames and GPS are
both timestamped by the PC when they arrive, so RTSP latency makes every
frame late relative to GPS. At 5 m/s, 300 ms is about 1.5 m. To measure
it, film a stopwatch running on this PC's screen and compare the stopwatch
with the Drone tab's live view. Enter the difference as **Drone connection
→ Video delay (ms)**.

While the drone is connected, the terminal also shows messages that are not
Studio errors — `[drone] drone says: …` (the autopilot's own status text)
and FFmpeg lines about the RTSP stream. See
[Troubleshooting](#troubleshooting).

**No drone at hand?** Try the whole loop on one PC:

```bash
cd rtvio
python tools/mock_drone.py --port 14550   # fake MAVLink drone: an armed copter flying a circle
```

Then in the Drone tab, set Drone IP to `127.0.0.1` and Video URL to the
full path of a local clip, e.g. `11240137-uhd_3840_2160_25fps.mp4`. The
clip is looped at its own frame rate.

## Field workflow: phone capture → desktop reconstruction

The Quickstart above needs no phone at all. This section is the full loop
for when you *do* want to capture with the Android app — install it, record
(with or without WiFi), get the footage onto this machine, and turn it into
a 3D model.

### 1. Install the app on your phone

<details>
<summary><strong>Get <code>adb</code> on your machine</strong> (skip if <code>adb devices</code> already works)</summary>

`adb` is the tool that installs the app and talks to a connected phone.

- **Already have Android Studio?** It's bundled at
  `<SDK>/platform-tools/adb.exe` — on Windows that's typically
  `C:\Users\<you>\AppData\Local\Android\Sdk\platform-tools`.
- **No IDE?** Install just the command-line tools and run
  `sdkmanager platform-tools`, which puts `adb` in the same
  `<SDK>/platform-tools` folder.
- Either way, **add that `platform-tools` folder to your PATH** so plain
  `adb devices` works from any terminal, instead of typing the full path
  every time:
  - Windows: Settings → System → About → Advanced system settings →
    Environment Variables → edit `Path` → add the `platform-tools` folder →
    open a new terminal.
  - macOS/Linux: add `export PATH="$PATH:$ANDROID_HOME/platform-tools"` to
    `~/.bashrc`/`~/.zshrc` and restart your shell.

</details>

1. **Enable USB debugging**: Settings → About phone → tap **Build number** 7
   times → back out → **Developer options** → enable **USB debugging**.
2. **Connect and authorize**: plug the phone in over USB, run `adb devices`,
   and accept the "Allow USB debugging?" prompt on the phone. Run it again —
   your device should now be listed.
3. **Build and install**:
   ```bash
   cd rtvioapk
   ./gradlew installDebug        # macOS/Linux
   .\gradlew.bat installDebug    # Windows (PowerShell or cmd)
   ```
   This needs `local.properties` pointing at your SDK first — see
   [`rtvioapk/README.md`](rtvioapk/README.md#3-point-the-build-at-your-sdk)
   if that step is new to you. The Gradle wrapper is included; the first
   run downloads Gradle 8.2 itself.
4. **Launch it**: tap the **RTVIO Mapper** icon, or
   `adb shell am start -n com.rtvio.mapper/.ui.MainActivity`. Grant the
   Camera and Location prompts — denying Location just disables GPS
   tagging, denying Camera blocks capture entirely.
5. **Point it at this PC**: Settings → **Server IP** = this PC's LAN address
   (RTVIO Studio prints it on start, and shows it on its Phone tab), **Server
   port** 5555.

### 2. Pick how you're going to capture

| | A. Record through RTVIO Studio | B. RECORD LOCALLY → Transfer | C. Live stream into `vggt_live` |
|---|---|---|---|
| **When** | This PC is reachable over WiFi | No route to the PC while capturing | Reachable, and you want the model the moment you stop |
| **On the phone** | **CONNECT**, then start/stop from the Studio page (or the phone's **● REC**) | **RECORD LOCALLY**, later ☰ → Saved sessions → **Transfer** | **CONNECT** (streaming starts at once) |
| **Frames kept** | All — the phone spools on its storage if WiFi falls behind | All | Fewer — see the path C tradeoff |
| **Reconstruction** | Automatic after each take (Studio) | Automatic after the transfer (Studio), or by hand | Window by window *while* you capture |

The app checks reachability on its own: **CONNECT** is only enabled when
something answers at Settings → Server IP; **RECORD LOCALLY** is always there.
**When in doubt, use A or B** — they keep every frame.

### 3. How to actually move the phone — read this before you fly

**This is the single biggest factor in reconstruction quality — far more
than resolution or frame rate.** VGGT recovers 3D structure from
*parallax*: the small shift in what the camera sees as it moves between
frames. No translation through the scene, no parallax, no reliable depth —
no matter how sharp the video looks.

✅ **Do**
- Move the camera smoothly *through* or *around* the scene — walk forward,
  orbit a subject, fly a path — with real translational motion, not just
  turning in place.
- Keep the subject close enough to fill a meaningful part of the frame,
  not a distant skyline hundreds of metres away.
- Aim for heavy frame-to-frame overlap (a common photogrammetry rule of
  thumb: **60–80%**) — move slowly enough, or shoot at a high enough frame
  rate, that consecutive frames look almost the same.
- Keep it to one continuous, steady pass.

❌ **Don't**
- Stand in one spot and pan/rotate to survey the surroundings — pure
  rotation gives the reconstruction no baseline to triangulate depth from,
  close to a worst case for monocular reconstruction.
- Whip-pan or jerk the camera — fast rotation blurs frames and breaks
  frame-to-frame matching.
- Linger on blank, textureless subjects (open sky, plain walls, still
  water) — there's nothing there for VGGT to match between frames.

Bumping video **resolution** won't help on its own: VGGT downsamples every
frame to ~518px internally regardless of what the app sends, which is why
720p is the app's (and the Studio's) default. A higher **frame rate** can
help a little (smaller motion between consecutive frames, less blur) — but
it can't substitute for real translational motion.

### 4. Capture, transfer, and reconstruct

#### A. Record through RTVIO Studio

**a. Start the Studio** and open the **Phone** tab:

```bash
cd rtvio
python -m rtvio.studio --open
```

**b. On the phone, tap CONNECT.** The Studio greets the app with protocol
v2, so the phone connects *armed*: its viewfinder appears on the Phone tab
and nothing is recorded yet.

**c. Set the Capture card** (resolution, frame rate, JPEG quality, **Record
GPS**, Record IMU). These are sent with each START and override the phone's
own settings for that take. **Record GPS is off by default** — tick it for an
outdoor take.

**d. Start recording → move through the scene → Stop recording**, on the
Studio page or with the phone's ● REC button. After STOP the phone uploads
anything it spooled (the button shows the count), then the take is saved to
`rtvio/data/sessions/<yyyyMMdd-HHmmss>/` and, with "reconstruct phone takes
automatically" ticked, queued on the GPU.

**e. Georeferencing.** Phone takes use **Reconstruction settings → GPS**,
which is **off (indoor)** by default: a take with GPS is still reconstructed
vision-only until you set it to **georeference (outdoor)** and press
**Reconstruct again**.

**f. View it**: **View cloud / View mesh** on the session card, or download
the PLY files from there.

#### B. RECORD LOCALLY → Transfer

**a. Record** — tap **RECORD LOCALLY**, move through the scene, tap **■ STOP
RECORDING**. No PC involved at all yet. GPS is recorded if the app's
Settings → **Outdoor mode** is on.

**b. Have a receiver running on the desktop** when you're back in WiFi
range. Either works:

- **RTVIO Studio** (`python -m rtvio.studio`): the transfer lands in
  `rtvio/data/sessions/<session-id>/`, shows up under Sessions, and is
  reconstructed automatically like path A.
- **`mock_receiver.py`**, if you'd rather reconstruct by hand (stop the
  Studio first — both use port 5555):
  ```bash
  cd rtvioapk
  python tools/mock_receiver.py --sessions-dir received_sessions
  ```

**c. Transfer**: toolbar (☰) → **Saved sessions** → tap the session →
**Transfer**. It arrives in the exact layout `vggt_reconstruct
--from-recording` reads — no conversion step. Keep the phone's copy until
you've confirmed the result looks right; the app offers to delete it after a
successful transfer, but never does so automatically. Details:
[`rtvioapk/README.md`](rtvioapk/README.md#recording-fully-offline).

**d. Reconstruct by hand** (only for the `mock_receiver.py` route):

```bash
cd rtvio
python -m rtvio.vggt_reconstruct --from-recording ../rtvioapk/received_sessions/<session-id> --extras --out data/outputs/<session-id>
python -m rtvio.view_output data/outputs/<session-id> --open
```

Georeferencing is automatic here: on only if the session has real GPS fixes
(`--gps-mode off` forces it off). `--extras` writes `mesh_poisson.glb`, which
`view_output` needs, plus `cloud.las` and a COLMAP export for gsplat.

#### C. Live stream → live VGGT reconstruction

**a. Start the live receiver *first*** — it needs to already be listening
before you connect. It uses port 5555 like the Studio, so stop the Studio
first:

```bash
cd rtvio
python -m rtvio.vggt_live --port 5555 --out data/outputs/live1 --extras
```

**b. On the phone, tap CONNECT.** This receiver doesn't speak the Studio's
remote-control protocol, so streaming starts at once. Reconstruction runs
window by window as frames arrive — you'll see a `window N: ...` line print
roughly every 8-15s once enough frames have accumulated for one.

**c. Move through the scene, then tap STOP STREAMING.** The receiver
processes whatever's left as a final window, then writes the same files a
batch run would straight into `data/outputs/live1` — no separate reconstruct
step.

**d. View it:**

```bash
python -m rtvio.view_output data/outputs/live1 --open
```

**The real tradeoff, from an actual measured run:** a 101s live session
delivered only 579 frames (~5.7 fps) versus the ~25 fps a RECORD LOCALLY
session gets. This isn't frames lost at the end — it's continuous: each
VGGT window blocks the receiver for 8-15s, during which the phone's
bounded video queue keeps dropping newly captured frames (evicts oldest
when full), so roughly 4-5 out of every 5 frames captured *during a
window* never reach the desktop. IMU/GPS still get through undropped. The
payoff is a finished model moments after you stop instead of minutes
later; the cost is reconstructing from a visibly sparser set of frames.
**When in doubt, use path A or B** — they keep every frame.

### 5. Reading the reconstruction log

Each `window N: ...` line, and the `CHECKPOINT_REPORT.md` written alongside
the output, describe how well consecutive windows of frames stitched
together. None of these are hard pass/fail gates in the code (except the
one noted below) — they're diagnostics:

| Field | What it measures | Healthy | Worth investigating |
|---|---|---|---|
| `X% px kept` | Share of the window's pixels confident + valid enough to fuse into the cloud | 30–50% | Under ~10% |
| `conf>=X` | That window's own confidence cutoff — meaningful only relative to neighbouring windows, not as an absolute number | — | — |
| `seam s=` | Scale factor aligning this window onto the previous one | Close to **1.0** | Far outside roughly 0.5–2.0 |
| `resid=` | Median alignment error of the seam fit, relative to depth | Under 0.05 | Above 0.1, and especially above 0.3 |
| `WARNING: ... fell back to single-camera chaining` | Fewer than 2000 confident shared pixels between windows — the *only* hard-coded threshold here (`MIN_ALIGN_CORRESPONDENCES`) | — | Any occurrence is a weak seam |

Frequent bad seams or fallback warnings point straight back to step 3:
fast pans, low-texture stretches, or too little real translation in that
part of the flight.

Full protocol/build/signing details: [`rtvioapk/README.md`](rtvioapk/README.md).

## Project layout

```
rtvio/          Python reconstruction engine + RTVIO Studio (drone and phone capture) — see rtvio/README.md
rtvioapk/       Android capture app (Kotlin) — see rtvioapk/README.md
SIH26158.pdf    the problem statement this project targets
*.mp4           sample test clips (real drone/phone footage) for the Quickstart above
```

## Documentation

| Question | Read |
|---|---|
| How does the pipeline actually work, file by file? | [`rtvio/README.md`](rtvio/README.md) |
| What changed and why (EKF removal, VGGT pivot, bug fixes)? | [`rtvio/CHANGELOG.md`](rtvio/CHANGELOG.md) |
| Phone↔desktop wire protocol details, three-lane live architecture | [`rtvio/docs/STREAMING.md`](rtvio/docs/STREAMING.md) |
| Camera intrinsics auto-discovery | [`rtvio/docs/CAMERA_INTRINSICS_INTEGRATION.md`](rtvio/docs/CAMERA_INTRINSICS_INTEGRATION.md) |
| Android app internals, wire protocol, release signing | [`rtvioapk/README.md`](rtvioapk/README.md) |
| Drone capture in RTVIO Studio: Indoor/Outdoor, session files, timestamps | [`rtvio/README.md` → Drone in RTVIO Studio](rtvio/README.md#drone-in-rtvio-studio) |
| How iDronam talks to the drone (ports, protocols) — where the Studio's drone connection details come from | [`rtvio/docs/IDRONAM_NOTES.md`](rtvio/docs/IDRONAM_NOTES.md) |
| The earlier SLAM/VIO/MVS/3DGS redesign plan (historical: written for the LingBot-Map direction, superseded by VGGT on 14 Sep 2026) | [`rtvio/docs/ARCHITECTURE_REDESIGN.md`](rtvio/docs/ARCHITECTURE_REDESIGN.md) |
| Raw development-session history (why decisions were made, in the moment) | [`rtvio/docs/dev_notes/`](rtvio/docs/dev_notes/) |

### The reconstruction paths, briefly

**VGGT batch (`rtvio.vggt_reconstruct`, recommended starting point):**

```bash
cd rtvio
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo
# with GPS (CSV: timestamp_s,lat_deg,lon_deg,alt_m) to georeference the result:
python -m rtvio.vggt_reconstruct --video clip.mp4 --gps track.csv --gps-mode global --out data/outputs/demo
# also emit LAS/OBJ/GLB/COLMAP alongside the default cloud_raw.ply + mesh_poisson.ply:
python -m rtvio.vggt_reconstruct --video clip.mp4 --out data/outputs/demo --extras
```

`python -m rtvio.vggt_reconstruct --help` lists every tuning flag; the top
of `rtvio/src/rtvio/vggt_reconstruct.py` explains what each stage does.

**Live VGGT (`rtvio.vggt_live`, needs the Android app streaming live):**

```bash
cd rtvio
python -m rtvio.vggt_live --port 5555 --out data/outputs/live1 --extras
# point the phone's Settings -> Server IP at this machine, then CONNECT
```

Same algorithm as `vggt_reconstruct --from-recording`, just fed frames as
they arrive instead of waiting for the recording to finish first — see
`rtvio/README.md`'s "Live VGGT reconstruction" for exactly what that saves
(and doesn't). Only useful for a live stream, not RECORD LOCALLY, which has no
network connection for this to receive over.

**Live streaming (`rtvio.live_pipeline`, needs the Android app):**

```bash
cd rtvio
# point the phone app's Settings -> Server IP at this machine, then:
python -u -m rtvio.live_pipeline --port 5555 --run-id NAME
# no GPS this run (e.g. indoors)?
python -u -m rtvio.live_pipeline --port 5555 --run-id NAME --indoor --live-viz
```

Always use `python -u` here — without it, stdout redirected to a file
shows nothing until the run ends.

**RTVIO Studio** — one browser UI for drone capture, phone capture and
video files, with the GPU job queue and a 3D viewer:

```bash
cd rtvio
python -m rtvio.studio --open      # http://127.0.0.1:8080; --help for ports
```

Drone tab: see [Field workflow: drone capture](#field-workflow-drone-capture-in-rtvio-studio).
Phone: see [path A](#a-record-through-rtvio-studio).

## Testing

```bash
cd rtvio
python tests/test_geometry.py              # camera-convention regressions (9)
python tests/test_stream.py                # live-ingest / wire-protocol acceptance checks (18)
python tests/test_relative_reinit.py       # two-view reinit (6)
python tests/test_pose_pipeline.py         # gyro/attitude/GPS-reanchor checks (18)
python tests/test_fusion.py                # VGGT window alignment, voxel fusion, PLY writers (23)
python tests/test_vggt_bridge.py           # phone-recording -> VGGT bridge checks (14)
python tests/test_drone_link.py            # drone link: MAVLink codec + mock-drone record/finalize (28)
python tests/test_camera_model.py          # lens calibration + undistortion on rendered fisheye checkerboards (27)
python tests/test_georeference_vggt.py     # GPS-noise Monte-Carlo sweep vs the 1m accuracy target (6)
```

All are plain scripts (no `pytest` required), CPU-only, a few seconds
each — none need a GPU or the VGGT checkpoint. The numbers in brackets are
the checks each one runs; all passed on the reference machine on 15 Sep 2026.
Run them before trusting any number the pipeline prints. The Android app's
19 unit tests run with `gradlew test` in `rtvioapk/`.

## Troubleshooting

**The clone has no `rtvio/pyproject.toml` / `vggt_reconstruct.py`** — you
cloned the default `main` branch. Run `git checkout rtvio-python-pipeline`
and `git submodule update --init --recursive`.

**`ModuleNotFoundError: No module named 'vggt'`** — the submodule wasn't
fetched. Run `git submodule update --init --recursive` from the repo root.

**`pip install` fails to find a wheel for `pymeshlab`/`torch`/etc.** —
try a virtual environment on Python 3.10–3.12, which have the widest
prebuilt-wheel coverage, if you're on something newer or on an uncommon
platform.

**`torch.cuda.is_available()` is `False`, or VGGT fails with "no kernel image
is available for execution on the device"** — the installed torch doesn't
match your GPU/driver. RTX 50-series cards need a CUDA 12.8+ build; reinstall
torch with the exact command from
[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)
rather than a plain `pip install torch` (on Windows that is CPU-only).

**First `vggt_reconstruct` run hangs or fails trying to reach
huggingface.co** — that's the automatic ~5GB checkpoint download; it needs
outbound network access. Download it once as in Installation step 6 (or
point `RTVIO_VGGT_CHECKPOINT` at a `.pt` file you already have).

**`view_output` page loads but shows a load error** — there's no
`mesh_poisson.glb` in that folder (it's only written with `--extras`), or
RTVIO Studio is already using port 8080 and the browser reached the Studio
instead: run `view_output` with `--port 8081`.

**The phone's CONNECT button is greyed out** — nothing answers at Settings →
Server IP/port. Check that the Studio (or another receiver) is running, the
phone is on the same WiFi, and Windows Firewall allows Python on private
networks (the phone port must be reachable from the LAN).

**Two receivers on port 5555** — RTVIO Studio, `vggt_live`, `live_pipeline`
and `mock_receiver.py` all default to port 5555. On Windows a second one can
start without an error, and the phone then reaches either one. Run one at a
time, or give one a different port (Studio: `--phone-port`; the others:
`--port`) and set the phone to match.

**Android build fails with "SDK location not found"** — `local.properties`
needs forward slashes even on Windows (`sdk.dir=C:/Users/you/.../Sdk`); a
backslash is parsed as an escape character. See `rtvioapk/README.md`.

**Phone take has no GPS** — for RECORD LOCALLY and live streaming, the app's
Settings → **Outdoor mode** must be on (it gates whether GPS collection
starts at all). For a take started from RTVIO Studio, the Studio's Capture
card → **Record GPS** decides instead, and it is off by default.

**Drone tab: "telemetry: cannot reach …:14550"**. Check the IP, and that
this PC is on the drone's network. If the connection is refused while
iDronam is connected, the drone may accept only one telemetry client at a
time: close iDronam and try again.

**Drone tab: telemetry works but no video ("video: cannot open rtsp://…")**.
Check **Drone connection → Video URL**. The default is the port and path
iDronam uses (`10000/drone_cam`). The stream is read by the FFmpeg that
ships inside `opencv-python`, so there's nothing else to install. VLC's
Open Network Stream is a quick way to test the URL on its own.

**Terminal fills with `[rtsp @ …] Illegal temporal ID in RTP/HEVC packet`**
(plus one `[hevc @ …] Could not find ref with POC …` right after the video
connects) — harmless. These lines come from the FFmpeg inside OpenCV, not
from RTVIO. The drone's RTSP server sends one malformed packet per keyframe
(every 2 s): the packet that bundles the stream's parameter sets (VPS/SPS/PPS)
carries a TemporalId of 0, which the HEVC-over-RTP standard (RFC 7798)
forbids, so FFmpeg drops it and logs this line. The same parameter sets
already arrive in the stream description, so no frame is lost — checked
against a real 184 s take. The single "Could not find ref" line is the
decoder joining the stream between keyframes; it stops at the first one.

**`[drone] drone says: PreArm: …` repeating** — the autopilot's own pre-arm
check messages (MAVLink STATUSTEXT), relayed to the terminal and the event
log. For example "Hardware safety switch" (the safety button hasn't been
pressed) or "Need Position Estimate" (no GPS position — normal indoors).
They stop the drone from arming, not the Studio from recording.

**Outdoor drone take shows "0 GPS fixes"**. GPS is recorded only while
telemetry is connected and the drone reports a 3D fix. Wait for "3D fix"
in the Drone tab's GPS row before pressing Start. A take like this is
reconstructed vision-only.

## Project status & known limitations

This is a hackathon-stage prototype, and the docs are deliberately candid
about what's verified versus what isn't rather than overstating either:

- The **VGGT batch path** has been run end-to-end on real drone footage
  successfully; a Monte-Carlo test (`test_georeference_vggt.py`) shows
  realistic consumer-GPS noise currently keeps georeferencing error above
  the competition's ≤1m target at the pipeline's default settings — a real,
  quantified, unsolved gap, not a bug (see `rtvio/CHANGELOG.md`).
- **Speed** on the reference machine (RTX 5070 Ti, 64-frame windows): VGGT
  runs at 4.4–7.0 frames/s across the real takes in `rtvio/data/`, i.e.
  0.10–0.23× real time. A 10-minute 30 fps video is ~43 minutes of VGGT at
  `--frame-stride 1` and ~14.5 minutes at `--frame-stride 3` — at the edge of
  PS-17's 15-minute budget before fusion and meshing.
- The **live streaming path** is honest monocular visual odometry
  (vision-driven pose, GPS re-anchoring) — not a tightly-coupled VIO. See
  `rtvio/README.md`'s "Pose comes from vision, not IMU" and "Known
  limitations" sections for exactly what that trades away.
- **Phone → RTVIO Studio remote recording is confirmed on real hardware**
  (vivo I2206, Android 14): a 93 s take (2,221 frames) was started and
  stopped remotely, saved, and reconstructed automatically.
- The **Android app has streamed from real phone hardware** — a 52.9s
  on-device test streamed 1,137 frames (21.5 fps avg) and 4,812 IMU samples
  (91.0 Hz avg) over WiFi to `mock_receiver.py` with a clean
  connect/disconnect.
- **RECORD LOCALLY and Saved sessions → Transfer are confirmed
  end-to-end on real hardware**: a 118.9s outdoor session (2,863 frames, 0
  dropped, 115 real GPS fixes) was captured fully offline, transferred over
  WiFi, and reconstructed by `vggt_reconstruct --from-recording` with no
  format conversion needed; transfers into RTVIO Studio work the same way.
  **The first reconstruction itself came out poor** — not a pipeline bug,
  but a capture-technique problem: the test footage panned a phone in place
  across a distant skyline (little to no parallax), which is close to a
  worst case for monocular reconstruction. See
  [Field workflow, step 3](#field-workflow-phone-capture--desktop-reconstruction)
  for what to do differently — move the camera through the scene, don't
  pivot it in place.
- **STREAMING → live reconstruction (`vggt_live.py`) is confirmed
  end-to-end on real hardware**: a 101s live session (579 frames received,
  11 windows, real GPS) streamed straight into a finished reconstruction
  141.1s after connecting (0.72x real time — most of the 83.8s of VGGT
  compute overlapped with the live capture) — no separate batch step. Two
  real issues turned up and were fixed rather than just noted: the app's own
  periodic reachability check (a bare connect-and-disconnect) was silently
  exiting the receiver before a real stream could ever arrive, and once
  fixed, the run exposed a genuine density tradeoff — only ~5.7 fps reached
  the desktop against RECORD LOCALLY's ~25 fps, because each ~8-15s VGGT
  window blocks the receiver and the phone's video queue drops frames
  while it waits. See [Field workflow, path C](#c-live-stream--live-vggt-reconstruction)
  for the number this cost on that run, and why paths A and B are the
  safer default when density matters more than turnaround time.
- **Drone capture in RTVIO Studio is confirmed on the real drone, indoors
  only.** On 15 Sep 2026 the Studio connected to the drone's ArduPilot
  telemetry and 1280×720 30 fps RTSP video, and recorded a 184 s take (5,520
  frames, 0 dropped) with the drone carried by hand — it wasn't flown
  (pre-arm checks were failing indoors). The take was reconstructed
  automatically: 817.6 s at 7.0 frames/s, 6.9 million points. 12 of its 98
  window seams fell back to single-camera chaining, a sign of fast turns or
  blank walls in that footage. `tools/mock_drone.py` and
  `tests/test_drone_link.py` cover the same path without hardware. Still to
  do on hardware:
  - an Outdoor flight with GPS fixes, georeferenced end to end
  - measure the video delay
  - calibrate the real camera's lens. Calibration is built and tested on
    checkerboards rendered through a known fisheye lens
    (`tests/test_camera_model.py`). The 184 s take above predates it,
    which is why its walls bend.
- **The Studio acts as a ground station toward the drone**, with the same
  identity iDronam uses (sysid 255). It sends a heartbeat every second, and
  on connect it asks the drone to stream its data, raising position and
  attitude to 10 times a second. If ArduPilot's ground-station failsafe is
  enabled and the Studio is the only ground station connected, closing it
  mid-flight can trigger that failsafe. Keep iDronam connected too, or
  close the Studio only on the ground.

## Contributing

See [`rtvio/CONTRIBUTING.md`](rtvio/CONTRIBUTING.md) for dev setup, test
conventions, and code-layout guidelines.

## License

**No license has been published yet.** Until one is added, this code is
shared for reference/evaluation (e.g. hackathon judging) under standard
copyright — all rights reserved by the author. Please contact the
repository owner before reusing, modifying, or redistributing it.

## Acknowledgments

- [VGGT](https://github.com/facebookresearch/vggt) (Meta/FAIR) — the
  transformer model the batch reconstruction path is built on, vendored
  here as a git submodule.
- [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) — dynamic
  object detection for masking.
- SIH 2026, problem statement PS-17 (`SIH26158.pdf`) — the brief this
  project targets.
