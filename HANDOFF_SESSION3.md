# Handoff - Session 3 (phone-rig bridge + remaining SIH26158 gaps)

Continuation of `HANDOFF_SESSION2.md`. Plan for this session:
`~/.claude/plans/okay-tell-me-what-ancient-puddle.md`. Same dev machine
(GTX 1650, 4GB VRAM, no nvcc) throughout - the RTX 5070 Ti and the user's
own drone (Suparna 5G / Menthosa, via iDronam GCS) telemetry integration are
both still pending on the user's side; this session used the `rtvioapk`
Android app as the near-term video+GPS source instead, per the user's
explicit direction.

This file is written incrementally as each plan item finishes - see the
plan doc for full context/reasoning on each.

## Item 1: Phone-session -> VGGT batch bridge (DONE, verified)

**1a. `live_pipeline.py --record-only`**: new flag, requires `--record DIR`.
When set, skips constructing `LiveReconstructor` entirely - the subscriber
list is just `[SessionRecorder(args.record, intrinsics)]`, so the socket
thread does nothing per packet but write-through to the recorder's bounded
queue. No more synchronous tracking/dense-stereo cost backpressuring the
phone during a real recording (the exact mechanism the original plan doc's
finding #2 diagnosed).

**1b. Refactored `vggt_reconstruct.reconstruct()`** into a thin
video-file-specific wrapper (`sample_video_frames` + `load_gps_track`) over
a new `_reconstruct_core(frame_paths, frame_times, gps_track, ...)` that
holds everything from GPS-track handling onward (windowing, VGGT,
georeferencing, merge, export) - agnostic to where the frames/GPS came
from.

**1c. New `reconstruct_from_recording(session_dir, out_dir, ...)`**: reads
a phone-recorded session fixture (`frames/*.jpg` + `frame_timestamps.json` +
`gps_data.json`, as written by `stream/recorder.py`'s `SessionRecorder`)
directly - no video re-encode, no hand-made CSV round-trip. New
`load_gps_track_from_recording` maps `gps_data.json`'s field names onto the
same shape `gps_enu_for_frames` already expects. Dropped frames (`None`
timestamp, no file on disk) are skipped, not treated as a gap-free
sequence. CLI: `python -m rtvio.vggt_reconstruct --from-recording DIR --out
OUT` alongside the existing `--video` path (now a mutually-exclusive
group).

**Real bug found and fixed along the way** (not part of the plan, found
while verifying it): `stream/recorder.py`'s `SessionRecorder.on_session_end`
unconditionally read `self.clock`, which is only ever set by
`on_session_start`. `StreamSession.run()` skips `on_session_start`
entirely when a session never establishes a usable clock offset (e.g. zero
IMU samples - see its "stream carried no usable pairing" `SystemExit`
path) but still calls `on_session_end` right before raising that -
crashing the recorder with an `AttributeError` and silently losing
whatever frames/GPS it *did* capture. This is exactly the first thing
someone verifying `--record-only` would hit (a short test recording with
sparse/no IMU). Fixed with a defensive `getattr(self, "clock", None)` -
now such a session ends cleanly (writes what it has, or an empty fixture)
instead of crashing.

**Verification performed**:
- Built a synthetic phone-session fixture (3 real frames from the test
  video + a deliberately-dropped 4th + a synthetic straight-line GPS
  track) and ran `reconstruct_from_recording` against it for real (actual
  VGGT forward pass, not mocked): correctly skipped the dropped frame,
  loaded GPS directly from `gps_data.json`, ran in **georeferenced mode**
  (3 anchors, residual mean=0.88m max=1.32m - encouraging against the ≤1m
  target), produced cloud/mesh/COLMAP output. Preserved at
  `rtvio/data/outputs/bridge_test/`.
- Exercised `--record-only` through the existing `--replay` machinery
  (`ReplayPacketSource` driving `SessionRecorder` with no
  `LiveReconstructor` present) against the same fixture, both with zero
  IMU samples (the degenerate path that surfaced the bug above - confirmed
  it now ends cleanly instead of crashing) and with IMU samples added
  (the normal clock-warmup path - confirmed a full fixture with correct
  frame/imu/gps counts gets written, and no `LiveReconstructor`/tracking
  output appears anywhere in the log).
- Confirmed the refactor is behavior-preserving: reran the *original*
  `reconstruct()` video-file path at the same settings as session 2's
  `masking_test` run - identical output (24069 points, matching exactly).
- New fast unit tests, `rtvio/tests/test_vggt_bridge.py` (8/8 passing, no
  GPU needed - only the frame/GPS-loading logic, not a real VGGT pass,
  which stays a manual/documented check per above): dropped-frame
  handling, too-few-frames rejection, GPS field mapping and sort order,
  missing-`gps_data.json` falling back to relative mode rather than
  erroring.
- Ran the full existing test suite (`test_geometry.py`, `test_pose_pipeline.py`,
  `test_relative_reinit.py`, `test_stream.py` - no pytest installed, run
  directly as scripts per their own convention) - all still pass, no
  regressions from the `live_pipeline.py`/`recorder.py` changes.

**What this does NOT do yet** (deliberately, see plan doc): consume
`imu_data.json` or `camera_intrinsics.json` from a recording (VGGT
self-estimates both; georeferencing only ever uses positions); the
pipelined/incremental reconstruction mode (start VGGT while recording is
still in progress) - user's call to defer, ship simple record-then-batch
first; and an actual test with the real `rtvioapk` app + a phone, which
needs the user's hardware, not mine.

**Files changed**: `rtvio/src/rtvio/live_pipeline.py` (`--record-only`),
`rtvio/src/rtvio/vggt_reconstruct.py` (core refactor + new entry point +
GPS-JSON loader), `rtvio/src/rtvio/stream/recorder.py` (the clock-attribute
crash fix), new `rtvio/tests/test_vggt_bridge.py`.

**Next step for the user**: run `rtvioapk`, point it at
`python -m rtvio.live_pipeline --record-only --record <dir>` on the
desktop, do a short real test (walk around with the phone is enough - a
drone isn't required to test this path), then run
`python -m rtvio.vggt_reconstruct --from-recording <dir> --out <out>` on
the result. That confirms the real socket/app path behaves like the
synthetic tests above - the one thing this session couldn't verify without
your hardware.

## Item 2: Processing-speed benchmark (DONE) - a real, important finding

Added per-window timing instrumentation to `_reconstruct_core` and fixed
`_write_outputs`'s "budget" line, which previously divided wall time by a
flat 900s regardless of the input video's actual length (meaningless on
any test clip that isn't ~10 minutes long, which none of this pipeline's
real test clips have been). It now reports realtime factor and a proper
projection to a 10-minute-equivalent video:

```
Wall time: 640.0 s for 45.0 s of input video (0.07x realtime). Projected
for a 10-minute video at this rate: 8533 s (9.48x SIH26158's 15-minute
budget)
Per-window time: min=19.2s mean=19.4s max=25.0s (n=30 windows)
```

**Real benchmark run** (not extrapolated from an 8-second clip like every
previous number in this project): 91 frames cycled from real footage at
the pipeline's actual default `SAMPLE_FPS=2.0` (previous timing numbers all
used `SAMPLE_FPS=25.0`, which nobody would actually use for a real
10-minute video), `use_masking=False` to isolate core VGGT/export cost,
`WINDOW_FRAMES=4`/`OVERLAP=1` (the real defaults) - 30 real windows, 640s
wall time. Preserved at `rtvio/data/outputs/speed_benchmark/`.

**Finding**: at this GPU's measured ~19.4s/window average and the
pipeline's default settings, a real 10-minute video projects to **~142
minutes (9.48x over SIH26158's 15-minute budget)**. To hit the budget on
*this* GPU alone (no hardware change), `SAMPLE_FPS` would need to drop from
2.0 to **~0.23** (one sampled frame every ~4.3s instead of every 0.5s) -
an ~8.6x reduction in temporal density, which would very likely hurt
Reconstruction Accuracy/Model Completeness (fewer, more widely-spaced
frames mean less overlap and coarser motion estimation) in exchange for
meeting the Processing Speed criterion - a real trade-off, not a free fix.
**Not changed this session** (SAMPLE_FPS stays at 2.0): changing the
default based on a projection, without measuring the actual quality
impact of sparser sampling, would be guessing at one number to fix another
without verifying either.

**What this means for hardware**: closing a ~9.5x gap needs either faster
per-window inference (the RTX 5070 Ti - bf16 already auto-detected,
untested magnitude of speedup) or a larger `WINDOW_FRAMES` amortizing
fixed per-call overhead across more frames per forward pass (only possible
with more VRAM) - both require the new hardware to actually measure, which
still isn't available this session.

**Files changed**: `rtvio/src/rtvio/vggt_reconstruct.py` (timing
instrumentation in `_reconstruct_core`, budget-line fix in
`_write_outputs`).

## Item 3: GPS-noise robustness test (DONE) - a real, important finding

New `rtvio/tests/test_georeference_vggt.py` (6/6 passing, CPU-only, no GPU
needed). Two things, since no committed test existed for
`so3.umeyama_alignment` at all before this:

1. **The regression test that should already have existed**: noiseless
   synthetic GPS recovers the true scale/rotation/translation to ~1e-8 and
   aligns points to sub-millimetre error. Confirms the math itself is
   correct - this was previously only an ad hoc, uncommitted script (see
   session 1's note).
2. **A Monte-Carlo sweep of realistic GPS noise** (200 trials per setting)
   against the pipeline's real per-window anchor count:

   | window anchors | horiz. GPS sigma | mean pos. error | max pos. error | ≤1m target? |
   |---|---|---|---|---|
   | 4  | 0.0m | 0.000m | 0.000m | yes |
   | 4  | 1.0m | 1.757m | 3.812m | **no** |
   | 4  | 3.0m | 5.065m | 11.179m | no |
   | 4  | 5.0m | 8.270m | 18.544m | no |
   | 10 | 1.0m | 1.086m | 2.030m | **no (barely)** |
   | 10 | 3.0m | 3.147m | 6.117m | no |
   | 10 | 5.0m | 5.106m | 10.241m | no |

**Finding**: with the pipeline's actual default window size
(`WINDOW_FRAMES=4`, so ≤4 GPS anchors per window), even a very good
consumer-GPS accuracy (1m 1-sigma) already produces ~1.76m mean position
error after alignment - over SIH26158's ≤1m spatial-accuracy target (30%
of the eval score, the single biggest criterion) before accounting for any
other error source (VGGT's own depth/pose noise, GPS altitude bias, etc.).
This is not a bug in `umeyama_alignment` - a least-squares fit cannot
recover more precision than the noise in its input allows, and doubling
the anchor count (10 vs. 4) only partially helps (1.09m vs. 1.76m mean at
1m noise - consistent with error shrinking roughly like sigma/sqrt(N), not
enough on its own to close a ~2x gap at realistic noise levels).

**Not fixed this session** (this is a real engineering trade-off, not a
quick patch):
- **Strongest concrete lever**: RTK/PPK corrections, which SIH26158 already
  lists as an OPTIONAL input. RTK-corrected GPS is typically centimetre-
  level, which per the same sigma/sqrt(N) relationship would put the
  ≤1m target well within reach even at n=4. Worth prioritizing if the
  competition's provided dataset includes it.
- Larger `WINDOW_FRAMES`/denser GPS sampling helps some (see n=10 above)
  but doesn't fully close the gap on its own at consumer-GPS noise levels.
- A global (not per-window) alignment fit using every GPS-tagged frame
  across the whole flight would average out far more noise (n in the
  hundreds, not 4-10) - NOT implemented or verified this session, since it
  conflicts with the reason per-window fitting was chosen in the first
  place (VGGT's own per-window scale/rotation isn't guaranteed consistent
  across windows - see `rigid_from_pose_pair`'s docstring). Flagging as a
  real idea, not a validated fix.

## Item 4: Fix dynamic-object masking (DONE - found a working fix)

Session 2 found stock `yolov8n-seg.pt` (COCO) detects zero dynamic objects
on nadir drone footage. This session found and verified a real fix:

**Found a working checkpoint**: [Mahadih534/YoloV8-VisDrone](https://huggingface.co/Mahadih534/YoloV8-VisDrone)
on HuggingFace - a YOLOv8 detector fine-tuned on VisDrone (aerial-viewpoint
imagery with `pedestrian/people/bicycle/car/van/truck/tricycle/
awning-tricycle/bus/motor` classes). Tested directly against the same
`frame_67.jpg` (3 visible parked cars) session 2 used:

| model | imgsz | conf | detections on the 3 real cars |
|---|---|---|---|
| stock COCO yolov8n-seg | any tested (640-3840) | any tested (0.1-0.25) | **0** |
| VisDrone yolov8 | 640 | 0.25 | 2 (conf 0.77, 0.39) |
| VisDrone yolov8 | 1280 | 0.25 | up to 8 boxes, real cars at conf 0.53-0.86 |

Visually confirmed via a before/after overlay
(`rtvio/data/outputs/masking_test/nadir_aerial_mask_overlay.jpg`): red
mask coverage now lands precisely on the parked cars, where session 2's
equivalent overlay had none at all.

**Wired into `ai_masking.DynamicMasker`**:
- `DynamicMasker.for_nadir_aerial()` - new alternate constructor: loads the
  VisDrone checkpoint (auto-downloaded to `data/models/` via
  `huggingface_hub` on first use, same "fetch once, cache locally"
  convention as the VGGT checkpoint), remaps `DYNAMIC_CLASSES` to
  VisDrone's own taxonomy (all 10 classes are dynamic by construction -
  unlike COCO there's no static subset to carve out), and sets
  `imgsz=1280` (measured to matter - cars are small in a wide nadir frame;
  640 caught only 2 of them, 1280 caught up to 8).
- Stock `DynamicMasker()` (COCO) stays the default - unchanged behavior for
  non-nadir footage, since this is genuinely a different use case, not a
  strict upgrade (VisDrone's classes/weights are tuned for aerial angles
  specifically).
- `vggt_reconstruct.py`: new `masking_preset` parameter
  (`"coco"` default / `"nadir_aerial"`), threaded through `reconstruct()`,
  `reconstruct_from_recording()`, and the CLI (`--masking-preset
  nadir_aerial`).

**Files changed**: `rtvio/src/rtvio/ai_masking.py` (the fix),
`rtvio/src/rtvio/vggt_reconstruct.py` (`masking_preset` plumbing),
`rtvio/pyproject.toml` (`huggingface_hub` added to the `masking` extra).

**Not done this session**: a full pipeline run with `masking_preset=
"nadir_aerial"` end-to-end (the masker itself is verified directly against
a real frame, which is what the original plan's Day-2 checkpoint actually
asked for - "before/after showing the mask actually removing them" - but a
full `reconstruct()` run with it enabled, to see the effect on final point
count/cloud quality, would be a good follow-up given more GPU time).

## Item 5: Minimal web viewer (DONE)

New `rtvio/src/rtvio/view_output.py` - a static file server (Python
stdlib `http.server`, no new dependency) plus one injected HTML page using
three.js (same r128/global-script convention as the existing
`viz_server.py`, for consistency - not a repurpose of it, since that
module is built entirely around a live SSE push from a running
reconstruction and there's nothing live here, just a finished `.glb`).
`GLTFLoader` loads `mesh_poisson.glb`, auto-fits the camera/grid/lights to
the mesh's own bounding box (works whether the scene is real-world-metre
scale or VGGT's small unanchored relative-mode units), `OrbitControls` for
interaction.

Usage: `python -m rtvio.view_output <out_dir> [--port 8080] [--open]`.

**Bug found and fixed while verifying**: the page template used Python
`%`-style formatting, but its CSS/JS legitimately contains literal `%`
characters (`height:100%`, etc.) that `%`-formatting misparses as format
specifiers - crashed immediately on startup (`ValueError: unsupported
format character ';'`). Switched to plain `.replace()` substitution, which
can't collide with template content the way `%`/`.format()` can.

**Verification performed**: started the server against a real output
directory (`rtvio/data/outputs/masking_test/`) and checked the actual HTTP
responses - `/` returns 200 with the right `text/html` content-type and
non-empty body, `/mesh_poisson.glb` returns 200 with the correct
`model/gltf-binary` content-type and byte-exact file size, a nonexistent
file correctly 404s. **Not done**: an actual visual render/screenshot in a
browser (no browser-automation tool was available this session, and it
declined to connect one) - the three.js loader/camera-fit logic is
reviewed and follows the same pattern `viz_server.py` already uses
successfully, but a human should open the page once to be sure. Quick
check for whoever does that: `python -m rtvio.view_output
rtvio/data/outputs/full_drone_test_201frames --open`.

**Files changed**: new `rtvio/src/rtvio/view_output.py`.

## Session summary

All 5 plan items done and verified to the extent possible without the
user's hardware (real drone/GCS telemetry, the RTX 5070 Ti, and a browser
for a final visual check on item 5). Two genuinely important findings this
session, both worth carrying into any demo/writeup:

1. **Processing speed is the biggest risk right now** (item 2): ~9.5x over
   budget on this GPU at real-world settings. This is a hardware/tuning
   problem, not a bug - re-measure immediately once the 5070 Ti is
   available, that's the single most informative thing to do next.
2. **Georeferencing accuracy needs more than per-window GPS alone** (item
   3): even good consumer GPS noise (1m) already exceeds the ≤1m target at
   the pipeline's real window size. RTK/PPK (already a PS-optional input)
   is the strongest lever found.

One real fix landed as a side effect of testing, not planned work: a
crash in `SessionRecorder.on_session_end` for short/IMU-less sessions
(item 1's verification surfaced it).
