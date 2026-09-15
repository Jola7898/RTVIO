# Contributing

## Setup

Follow the root [`README.md`](../README.md#installation) Installation steps:
clone the `rtvio-python-pipeline` branch with its submodule, then

```powershell
cd rtvio
python -m pip install -e .
# PyTorch for your GPU (see the root README, step 4), then:
python -m pip install -e ".[vggt,mesh,masking]"
```

This installs `rtvio` in editable mode, so edits under `src/rtvio/` take
effect immediately without reinstalling — the `import rtvio` package and the
`rtvio-live` / `rtvio-reconstruct` console scripts all point at the checkout,
not a copy. Re-run `pip install -e .` after `pyproject.toml` changes (new
console scripts are only registered at install time).

## Running the tests

```powershell
python tests/test_geometry.py          # camera-convention regressions (9)
python tests/test_stream.py            # live-ingest / wire-protocol acceptance checks (18)
python tests/test_relative_reinit.py   # two-view relative-pose reinit (6)
python tests/test_pose_pipeline.py     # gyro integration, attitude init, GPS re-anchor (18)
python tests/test_fusion.py            # VGGT window alignment, voxel fusion, PLY writers (23)
python tests/test_vggt_bridge.py       # phone recording -> VGGT bridge (14)
python tests/test_drone_link.py        # MAVLink codec + mock-drone record/finalize (28)
python tests/test_camera_model.py      # lens calibration + undistortion on rendered fisheye boards (27)
python tests/test_georeference_vggt.py # GPS-noise sweep vs the 1 m target (6)
```

All are plain scripts (a small custom PASS/FAIL harness, no pytest required),
CPU-only, and take a few seconds each, so a failure's traceback points straight
at the assertion. Run them before trusting any number the pipeline prints — see
`README.md`'s "Why `tests/test_geometry.py` exists" for what they protect
against. The Android app has its own unit tests: `cd ../rtvioapk` then
`gradlew test`.

## Code layout

See the "Project layout" section in `README.md`. In short: pipeline code
lives in `src/rtvio/`, RTVIO Studio in `src/rtvio/studio/`, tests in `tests/`,
standalone CLI utilities in `tools/`, and runtime data (intrinsics, models,
sessions, run outputs) in `data/`. Within `src/rtvio/`, use relative imports
(`from .tracking import ...`, `from .stream.geodesy import ...`) for anything
intra-package.

## Documentation

- The root `README.md` is the entry point (install, quickstart, field
  workflows); `rtvio/README.md` covers the pipeline file by file. Keep both in
  sync with any change to the CLI, the Studio UI, output layout, or
  dependencies.
- `docs/STREAMING.md` documents the live-ingest architecture in depth;
  update it when the three-lane design, hazards, or measured numbers change.
- `docs/dev_notes/` holds raw development session transcripts kept for
  historical context (how a number was measured, why a design was chosen).
  Don't edit these after the fact - add a new note or update `docs/` instead.
