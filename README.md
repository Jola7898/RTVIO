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
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Project status & known limitations](#project-status--known-limitations)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

## What's in here

Two halves that talk to each other over a WiFi socket:

- **`rtvio/`** — the Python reconstruction engine. Two independent
  reconstruction paths live here: a **batch pipeline** built on Meta's
  [VGGT](https://github.com/facebookresearch/vggt) transformer model
  (the actively-developed, more-accurate path), and an older **live
  streaming pipeline** that produces a pose in real time as the phone
  flies, for "situational awareness" while a flight is in progress.
- **`rtvioapk/`** — an Android app (Kotlin) that captures the phone's
  camera, IMU, and GPS and streams them over WiFi to `rtvio/`. The phone
  does capture and transmission only; all reconstruction happens on the
  desktop/GPU side.

Two sample video clips at the repo root (real drone/phone footage) let you
try the pipeline immediately without a phone.

## Features

- Georeferenced dense point cloud and a true 3D textured mesh (Screened
  Poisson surface reconstruction — overhangs and vertical faces included,
  not a flat heightmap) from a single drone pass
- Batch reconstruction via VGGT (joint multi-view depth + pose, no COLMAP
  needed) and a separate real-time monocular vision pipeline with GPS
  re-anchoring (2.5D heightmap mesh + DSM raster — see "Two reconstruction
  paths" below for why there are two, and what each actually outputs)
- Android capture app streaming video + IMU + GPS over WiFi — no dedicated
  flight-controller integration required
- Camera intrinsics auto-discovered from the phone's Camera2 API, with a
  manual checkerboard-calibration fallback tool
- Dynamic-object (people/vehicle) masking via YOLO before reconstruction
- Exports to LAS (point cloud), OBJ/GLB (mesh), a georeferenced DSM raster,
  and a COLMAP dataset (for downstream tools like gsplat)
- **RTVIO Studio** — a single browser UI that drives phone capture, queues
  GPU reconstructions, and opens the result in a 3D viewer

## Requirements

| | |
|---|---|
| OS | Developed and tested on Windows 10/11. The Python core has no Windows-only code paths, but Linux/macOS are untested — see [Troubleshooting](#troubleshooting). |
| Python | 3.10+ (3.10–3.11 recommended for the widest prebuilt-wheel availability; developed against 3.13) |
| Git | with submodule support (any reasonably recent Git) |
| Disk space | ~10GB free — the VGGT-1B checkpoint (~5GB) downloads on first use |
| GPU | Optional but strongly recommended for the VGGT batch path — NVIDIA + CUDA. A CPU fallback exists (correct, just slow). Tested on a 4GB GTX 1650 and a 16GB RTX 5070 Ti. |
| Android app (optional) | Android Studio (bundles a suitable JDK + SDK manager), or JDK 17 + Android SDK platform 34 command-line tools. A physical Android 7.0+ (API 24+) phone if you want to stream live. |

## Installation

**1. Clone the repository, including its submodule** (the VGGT model code lives in a submodule — a plain `git clone` alone leaves that folder empty):

```bash
git clone --recurse-submodules https://github.com/Jola7898/RTVIO.git
cd RTVIO
```

Already cloned without `--recurse-submodules`? Fetch it now:

```bash
git submodule update --init --recursive
```

**2. Create and activate a virtual environment** (recommended, not required):

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

**3. Install the core package** (editable install — code changes take effect immediately, no reinstall needed):

```bash
cd rtvio
pip install -e .
```

**4. Install PyTorch** — needed for the VGGT batch path. There's no single right answer here since it depends on your GPU, which is why it isn't pinned in `pyproject.toml`; get the exact command for your machine from **[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)**. For example, CUDA 12.1:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

CPU-only (works, just much slower for VGGT):

```bash
pip install torch torchvision
```

**5. Install the optional extras** (VGGT model loading, meshing/export, YOLO masking):

```bash
pip install -e ".[vggt,mesh,masking]"
```

**6. Verify the install** — these are fast, CPU-only, no GPU or checkpoint needed:

```bash
python tests/test_geometry.py
python tests/test_stream.py
```

Both should print an all-pass summary. If they do, the core install is good.

## Quickstart

See a real reconstruction with zero setup beyond the steps above, using one
of the sample clips at the repo root:

```bash
cd rtvio
python -m rtvio.vggt_reconstruct --video ../11240137-uhd_3840_2160_25fps.mp4 --out data/outputs/quickstart
python -m rtvio.view_output data/outputs/quickstart --open
```

The first run downloads the ~5GB VGGT-1B checkpoint automatically (needs
network access — see [Troubleshooting](#troubleshooting) if it can't), and
saves it locally so every run after that is fast. `view_output` opens a
browser tab with a 3D viewer over the finished `mesh_poisson.glb`.

## Usage

This file only gets you to a first working run. For everything past that —
the live streaming pipeline, RTVIO Studio, the Android app, calibration
tools — see the [Documentation](#documentation) table below, which points
at the file that actually covers each one in depth.

## Project layout

```
rtvio/          Python reconstruction engine — see rtvio/README.md
rtvioapk/       Android capture app (Kotlin) — see rtvioapk/README.md
SIH26158.pdf    the problem statement this project targets
*.mp4           sample test clips (real drone/phone footage) for the Quickstart above
```

## Documentation

| Question | Read |
|---|---|
| How does the pipeline actually work, file by file? | [`rtvio/README.md`](rtvio/README.md) |
| Why two reconstruction paths, and how do SLAM/VIO/MVS/3DGS fit together? | [`rtvio/docs/ARCHITECTURE_REDESIGN.md`](rtvio/docs/ARCHITECTURE_REDESIGN.md) |
| What changed and why (EKF removal, VGGT pivot, bug fixes)? | [`rtvio/CHANGELOG.md`](rtvio/CHANGELOG.md) |
| Phone↔desktop wire protocol details, three-lane live architecture | [`rtvio/docs/STREAMING.md`](rtvio/docs/STREAMING.md) |
| Camera intrinsics auto-discovery | [`rtvio/docs/CAMERA_INTRINSICS_INTEGRATION.md`](rtvio/docs/CAMERA_INTRINSICS_INTEGRATION.md) |
| Android app internals, wire protocol, release signing | [`rtvioapk/README.md`](rtvioapk/README.md) |
| Raw development-session history (why decisions were made, in the moment) | [`rtvio/docs/dev_notes/`](rtvio/docs/dev_notes/) |

### The two reconstruction paths, briefly

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

**RTVIO Studio** — one browser UI for both:

```bash
cd rtvio
python -m rtvio.studio            # then open http://127.0.0.1:8080
```

## Testing

```bash
cd rtvio
python tests/test_geometry.py              # camera-convention regressions
python tests/test_stream.py                # live-ingest / wire-protocol acceptance checks
python tests/test_relative_reinit.py
python tests/test_pose_pipeline.py         # gyro/attitude/GPS-reanchor checks
python tests/test_fusion.py                # VGGT window-alignment + PLY-writer checks
python tests/test_vggt_bridge.py           # phone-recording -> VGGT bridge checks
python tests/test_georeference_vggt.py     # GPS-noise Monte-Carlo sweep vs the 1m accuracy target
```

All are plain scripts (no `pytest` required), CPU-only, a few seconds
each — none need a GPU or the VGGT checkpoint. Run them before trusting
any number the pipeline prints.

## Troubleshooting

**`ModuleNotFoundError: No module named 'vggt'`** — the submodule wasn't
fetched. Run `git submodule update --init --recursive` from the repo root.

**`pip install` fails to find a wheel for `pymeshlab`/`torch`/etc.** —
prebuilt-wheel coverage is best on Python 3.10–3.11; try a virtual
environment on one of those versions if you're on something newer or on an
uncommon platform.

**A CUDA/PyTorch version mismatch, or `torch.cuda.is_available()` is
`False` on a machine with a GPU** — reinstall torch using the exact command
for your CUDA version from [pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/)
rather than a generic `pip install torch`.

**First `vggt_reconstruct` run hangs or fails trying to reach
huggingface.co** — that's the automatic ~5GB checkpoint download; it needs
outbound network access. To use a checkpoint you already have locally
instead, point `RTVIO_VGGT_CHECKPOINT` at the `.pt` file, or place it at
`rtvio/data/models/vggt1b_model.pt`.

**Android build fails with "SDK location not found"** — `local.properties`
needs forward slashes even on Windows (`sdk.dir=C:/Users/you/.../Sdk`); a
backslash is parsed as an escape character. See `rtvioapk/README.md`.

**Phone never sends a GPS fix** — the app's Settings screen has a separate
Indoor/Outdoor toggle that gates whether GPS collection starts at all; it
must be set to Outdoor for a real flight.

## Project status & known limitations

This is a hackathon-stage prototype, and the docs are deliberately candid
about what's verified versus what isn't rather than overstating either:

- The **VGGT batch path** has been run end-to-end on real drone footage
  successfully; a Monte-Carlo test (`test_georeference_vggt.py`) shows
  realistic consumer-GPS noise currently keeps georeferencing error above
  the competition's ≤1m target at the pipeline's default settings — a real,
  quantified, unsolved gap, not a bug (see `rtvio/CHANGELOG.md`).
- The **live streaming path** is honest monocular visual odometry
  (vision-driven pose, GPS re-anchoring) — not a tightly-coupled VIO. See
  `rtvio/README.md`'s "Pose comes from vision, not IMU" and "Known
  limitations" sections for exactly what that trades away.
- The **Android app compiles and passes its unit tests, but has never run
  on real phone hardware** — the camera pipeline, sensor rates, GPS, and
  live socket are unverified on-device. Treat the first real-phone run as
  the actual test, not the build succeeding.

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
