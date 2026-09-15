# Handoff - VGGT pivot session (read this first)

Session ran out of credits mid-task. Everything below is real, verified state
- not a plan, a record of what actually happened. Start here.

**UPDATE, last thing done this session**: the COLMAP-export real-data
confirmation run (mentioned as "in progress" below) **finished and
succeeded**: 9 cameras/images, 24069 points, valid cameras.txt/images.txt/
points3D.txt + images/. Preserved at
`rtvio/data/outputs/colmap_confirm_real/colmap/`. So the COLMAP export
feature is fully confirmed working against real data, not just synthetic -
treat the "in progress" note below as resolved/done.

## Where things stand, in one paragraph

The old hand-built tracking/EKF pipeline is diagnosed and abandoned (see
`~/.claude/plans/here-i-have-a-fancy-treasure.md` for the full diagnosis of
why it produced duplicated point clouds and was 60x too slow). It's been
replaced with `rtvio/src/rtvio/vggt_reconstruct.py`, a new batch pipeline
built on VGGT (facebookresearch/vggt, vendored at `rtvio/third_party/vggt/`).
**This new pipeline has been run successfully end-to-end on real drone
footage, all 201 frames of it, producing a real 49,432-point cloud and two
kinds of mesh.** Output saved at
`rtvio/data/outputs/full_drone_test_201frames/` (look at `trajectory_check.png`
and open `cloud_raw.ply`/`mesh_poisson.glb` to see it).

## Hardware situation (important context for any perf decision)

- Dev machine (where all this ran): GTX 1650, **4GB VRAM**. This forced
  `WINDOW_FRAMES=4` (measured: n=8 already exceeds VRAM and silently
  degrades to ~50-100x slower via Windows' shared-memory spillover, no
  error raised - see comments in `vggt_reconstruct.py` and the plan doc).
- **User will do real work on an RTX 5070 Ti, 16GB VRAM, once available.**
  Code already auto-detects compute capability and will use bf16 + could
  use larger `WINDOW_FRAMES` there - that constant is the first thing to
  re-measure on the new hardware (just re-run the VRAM/timing sweep the
  plan doc describes).
- No CUDA compiler (nvcc/toolkit) installed on this dev machine - this is
  why Gaussian Splatting training was deferred (see below).

## What's built and verified working

`rtvio/src/rtvio/vggt_reconstruct.py` - the whole new pipeline. Key pieces,
all tested against real data, not just written:

1. **`_load_vggt`**: loads the VGGT-1B checkpoint from
   `rtvio/data/models/vggt1b_model.pt` (5GB, gitignored, already downloaded
   on this machine - **if this file is missing on a fresh machine**, either
   re-download from `huggingface.co/facebook/VGGT-1B/resolve/main/model.pt`
   via curl (huggingface_hub's own downloader was too slow/flaky on this
   network - see git history) and place it there, or set env var
   `RTVIO_VGGT_CHECKPOINT` to point elsewhere.
   - Uses `mmap=True` on `torch.load` (plain load ballooned to 8GB+ RAM).
   - Casts **only the aggregator** submodule to fp16, keeps camera_head/
     depth_head in fp32 (whole-model fp16 overflows depth_head's exp()-based
     confidence activation -> NaN everywhere; confirmed and fixed this
     session, see the long comment in `_load_vggt` and `run_window`).
   - Runs the heads genuinely OUTSIDE `torch.autocast` (autocast wrapping
     them ALSO caused the NaN issue even with fp32 parameters - non-obvious,
     confirmed by testing, documented in `run_window`'s comments).

2. **`run_window`**: one VGGT forward pass over a small batch of frames.
   Returns per-frame camera pose (position+rotation), confidence-filtered
   3D points+colors, intrinsics, and the actual preprocessed image tensor.

3. **Georeferencing, two modes**:
   - **GPS mode**: `so3.umeyama_alignment` fits a similarity transform
     (rotation+translation+scale) between VGGT's camera positions and
     GPS-derived ENU positions per window (needs >=3 GPS points in-window).
   - **Relative mode (no GPS)**: `so3.rigid_from_pose_pair` chains each
     window onto the previous one using their ONE shared overlapping frame's
     full 6-DOF pose (not just position) - this is what let `WINDOW_OVERLAP`
     stay at 1 instead of needing >=3 for a real fit. Verified against
     synthetic ground truth (exact to float precision) AND against the real
     67-window drone run (see below) - produces one continuous cloud, not
     67 disconnected islands (which is what it did before this fix).
   - Known weakness of relative mode: scale isn't re-verified between
     windows (assumes VGGT's own metric-scale consistency), so it can drift
     over many chained windows - visible as slight vertical spread in the
     67-window test's side-view render. Real GPS avoids this entirely.

4. **Adaptive voxel/cell sizing in relative mode**: without GPS, "metres"
   has no defined meaning (VGGT's own scale for a real aerial scene came out
   as ~1.5 arbitrary units wide, not metres) - a fixed `voxel_size_m=0.3`
   collapsed a 2.9M-point cloud to 37 points. Now auto-derives voxel/cell
   size from the actual point cloud extent when `georeferenced=False`.

5. **COLMAP export** (`export_colmap`, `_collect_colmap_frames`): writes a
   plain-text COLMAP sparse dataset (`cameras.txt`/`images.txt`/
   `points3D.txt` + `images/`) to `<out_dir>/colmap/`, for later Gaussian
   Splatting training via `gsplat`. Deliberately hand-written, NOT using
   `pycolmap` (avoids another "no Windows wheel" risk this codebase has hit
   before). **Verified against synthetic data** (correct file format, right
   frame count/dedup). **A real-data confirmation run was in progress when
   the session ended** (`run_colmap_confirm.py`, 3-window quick test) - check
   `C:\Temps\claude\vggt_colmap_confirm\out\colmap\` for its result; if it's
   not there, just re-run that script (see "How to resume" below).

## Bugs found and fixed this session (don't re-discover these)

1. Default CUDA torch install was CPU-only; default `cu124` package index
   has no build for this GPU/Python(3.13)/torch(2.9) combo - had to use the
   `cu126` index specifically.
2. fp32 model (~5GB) exceeds this card's 4GB VRAM; Windows silently spills
   to slow shared memory instead of erroring - only found by checking
   `torch.cuda.memory_allocated()` against the card's actual total.
3. Whole-model fp16 -> NaN in depth confidence (see above).
4. `aggregated_tokens_list` has legitimate `None` entries (typed
   `List[Optional[Tensor]]` in VGGT's own source) - crashed on a naive cast.
5. `voxel_size_m=0.3` (inherited from the old aerial-scale pipeline) is
   meaningless without GPS - see adaptive sizing above.
6. **Windows text-mode file writing bug**: `open(path, "w")` on Windows
   silently converts `\n` to `\r\n`, which broke a strict PLY reader
   (SuperSplat) that searches for the exact byte sequence `end_header\n`.
   Fixed in `vggt_reconstruct.py`'s `cloud_raw.ply` writer via
   `newline=""`. **If you add any other hand-written text file output,
   use `newline=""` or `newline="\n"` on the `open()` call.**

## Gaussian Splatting - decision made, partially executed

User explicitly wants Gaussian Splatting output (saw it elsewhere, prefers
it over plain mesh/point cloud - **note the actual SIH26158 spec only asks
for "3D Mesh / Point Cloud", splatting is a bonus/nice-to-have, not a
requirement**). Checked feasibility: `gsplat` (the standard trainer) needs
a CUDA compiler to build custom kernels; this dev machine has none. Decision
(user-approved): **export data in COLMAP format now (done, see above),
defer actual `gsplat` training to when the RTX 5070 Ti is available** (real
CUDA compiler environment, much more VRAM, faster).

**Next steps for Gaussian Splatting, once on the 5070 Ti machine**:
1. Confirm/re-run the COLMAP export against a real, full-length capture
   (ideally with real GPS this time for a properly-scaled result).
2. Install `gsplat` (`pip install gsplat`) - should compile fine with a
   real CUDA toolkit + MSVC present.
3. `python examples/simple_trainer.py default --data_dir <out>/colmap
   --data_factor 1 --result_dir <result>` (gsplat's own example trainer,
   reads the COLMAP-format dataset directly).

## Day 2 work (not started this session)

- `ai_masking.py`'s `DynamicMasker` (YOLOv8-seg, masks moving
  people/vehicles) exists and is wired into `run_window` (see
  `masker.get_static_mask` call), but has **never been exercised** in this
  new VGGT pipeline - every real run this session used `use_masking=False`
  to isolate core reconstruction quality first. Test it next: rerun with
  masking enabled on a clip that has visible people/cars, confirm the mask
  actually removes them (before/after point count or visual check).
- FBX export not implemented (OBJ/PLY/LAS/glTF all are - covers the format
  requirement already).

## How to resume

**Test data**: `11240137-uhd_3840_2160_25fps.mp4` at the repo root - real
top-down drone footage, 8s/201 frames/25fps, no GPS available for it.

**Re-run the full pipeline** (takes ~25 min on the 4GB dev card, should be
much faster on the 5070 Ti):
```
cd rtvio
python -c "
import sys; sys.path.insert(0, 'src')
from rtvio.vggt_reconstruct import reconstruct
reconstruct(
    video_path=r'../11240137-uhd_3840_2160_25fps.mp4',
    gps_path=None, out_dir=r'data/outputs/rerun',
    sample_fps=25.0, use_masking=False,
)
"
```
Or via CLI: `python -m rtvio.vggt_reconstruct --video <path> --out <dir>
[--gps <csv>] [--no-masking] [--voxel-size-m 0.3]`.

**Check the in-progress COLMAP confirmation run**: look for
`C:\Temps\claude\vggt_colmap_confirm\out\colmap\sparse\0\cameras.txt` etc.
If missing/incomplete, just rerun (it's a fast 3-window test, few minutes):
```
python -c "
import sys; sys.path.insert(0, 'src')
from rtvio.vggt_reconstruct import reconstruct
reconstruct(video_path=r'../11240137-uhd_3840_2160_25fps.mp4', gps_path=None,
    out_dir=r'data/outputs/colmap_confirm', sample_fps=1.0, use_masking=False)
"
```

**Everything under `C:\Temps\claude\...` is session-scratch and may be
gone** - the important outputs were copied to
`rtvio/data/outputs/full_drone_test_201frames/` before this session ended;
treat that as the durable copy.

## Files changed this session (for a diff/review)

- `rtvio/src/rtvio/vggt_reconstruct.py` - new file, the whole pipeline
- `rtvio/src/rtvio/so3.py` - added `umeyama_alignment`, `rigid_from_pose_pair`
- `rtvio/pyproject.toml` - added `vggt`/`mesh`/`masking` optional-dep groups
- `rtvio/.gitignore` - added `data/models/*.pt`
- `rtvio/third_party/vggt/` - vendored VGGT repo (git clone, untracked in
  this repo's own git unless explicitly added)
- `rtvio/data/models/vggt1b_model.pt` - the 5GB checkpoint (gitignored)
- `rtvio/data/outputs/full_drone_test_201frames/` - preserved real output
- `~/.claude/plans/here-i-have-a-fancy-treasure.md` - the original diagnosis
  and plan doc, still accurate for the "why" of all this

Nothing has been committed to git yet - working tree has all these changes
uncommitted. Review and commit when ready.
