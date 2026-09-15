# Contributing

## Setup

```powershell
cd rtvio
python -m pip install -e .
```

This installs `rtvio` in editable mode, so edits under `src/rtvio/` take
effect immediately without reinstalling — both the `import rtvio` package
and the `rtvio-live` console script point at the checkout, not a copy.

## Running the tests

```powershell
python tests/test_geometry.py        # camera-convention regressions, ~2s
python tests/test_stream.py          # live-ingest acceptance checks, ~2s
python tests/test_relative_reinit.py # two-view relative-pose reinit, ~1s
python tests/test_pose_pipeline.py   # gyro integration, attitude init, GPS re-anchor, ~1s
```

Both are plain scripts (a small custom PASS/FAIL harness, no pytest
required) so a failure's traceback points straight at the assertion. Run
them before trusting any number the pipeline prints — see the README
section "Why `tests/test_geometry.py` exists" for what they're protecting
against.

## Code layout

See the "Project layout" section in `README.md`. In short: pipeline code
lives in `src/rtvio/`, tests in `tests/`, standalone CLI utilities in
`tools/`, and runtime data (intrinsics, calibration frames, run outputs) in
`data/`. Within `src/rtvio/`, use relative imports (`from .tracking import
...`, `from .stream.geodesy import ...`) for anything intra-package.

## Documentation

- `README.md` is the entry point — keep it in sync with any change to the
  CLI, output layout, or dependencies.
- `docs/STREAMING.md` documents the live-ingest architecture in depth;
  update it when the three-lane design, hazards, or measured numbers change.
- `docs/dev_notes/` holds raw development session transcripts kept for
  historical context (how a number was measured, why a design was chosen).
  Don't edit these after the fact - add a new note or update `docs/` instead.
