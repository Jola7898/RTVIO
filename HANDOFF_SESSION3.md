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
