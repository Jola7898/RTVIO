# Handoff - Session 2 (Day 2 masking verification)

Continuation of `HANDOFF_SESSION1.md`. Same dev machine (GTX 1650, 4GB VRAM,
no nvcc) - the RTX 5070 Ti was not available this session either, so the
gsplat-training next-step from session 1 is still deferred, untouched.

This session did exactly one thing from session 1's "Day 2 work" list:
**exercised `ai_masking.py`'s `DynamicMasker` for the first time in the new
VGGT pipeline**, per the explicit instruction to test it before assuming it
works.

## Finding: dynamic-object masking does not work on nadir drone footage

Verified two ways against the real test clip
(`11240137-uhd_3840_2160_25fps.mp4`), which has clearly visible parked cars
in several frames (e.g. frame 67):

1. **Direct unit test**: called `DynamicMasker.get_static_mask` on that frame
   directly. Result: **0 of 8.29M pixels flagged as dynamic** - the mask is
   the trivial all-static default. Confirmed visually too (see
   `rtvio/data/outputs/masking_test/mask_test_frame67_overlay.jpg` - red
   overlay would mark detected pixels; there is none over the 3 visible
   cars).
2. **Full pipeline run**: ran `reconstruct(..., use_masking=True)` end-to-end
   (`sample_fps=1.0`, 9 frames / 3 windows - same shape as session 1's COLMAP
   confirmation run). **No crash** - the masking code path in `run_window`
   (image -> mask -> zero out -> re-save -> feed to VGGT) is wired correctly
   and runs clean. Output: 24546 points, COLMAP dataset written, mesh
   written - consistent with session 1's unmasked 24069-point run on
   equivalent settings, which makes sense given finding #1: nothing gets
   masked, so nothing should change. Preserved at
   `rtvio/data/outputs/masking_test/`.

**Root cause**: `DynamicMasker` uses stock `yolov8n-seg.pt`, COCO-trained on
ground-level/oblique photography. Swept confidence threshold (0.25 -> 0.1)
and input resolution (640 -> 3840, i.e. native res) directly against the
underlying YOLO model outside the wrapper - detections never meaningfully
improve. At the loosest settings tested, actual cars get misclassified as
"train" (0.27 conf), "truck" (0.16), "bird", or "airplane" - never a clean,
usable detection. This is a known general failure mode: a car viewed
directly from above (nadir) looks nothing like COCO's side/oblique training
images of cars, so the detector doesn't generalize to this viewing angle
no matter how the confidence/resolution knobs are turned.

**This is not a wiring bug** - the integration in `vggt_reconstruct.py` is
correct and does nothing wrong. The masking *feature*, as currently
specified (stock COCO YOLOv8-seg), simply does not do useful work on nadir
aerial capture, which is this project's primary use case (SIH26158 is a
drone-mapping spec). Ground-level or oblique-angle footage would likely fare
better with the same code - untested this session, no such clip available.

**Not fixed this session** - flagging for a decision rather than guessing:
a real fix would mean swapping in a detector trained on aerial/nadir
imagery (e.g. a YOLO variant fine-tuned on VisDrone or DOTA, which do
include vehicle classes from overhead angles), which is a model-sourcing +
integration task of its own, not a quick tweak. Left `DynamicMasker`'s
`DYNAMIC_CLASSES`/thresholds unchanged since no tested configuration of the
*current* model actually helps.

## Small hardening fix made along the way

`DynamicMasker.__init__` previously passed a bare filename (`'yolov8n-seg.pt'`)
straight to `ultralytics.YOLO(...)`, which downloads it to the process's
current working directory - so the weight file landed in different places
depending on where a script was launched from (observed both at the repo
root and at `rtvio/` this session). Changed it to resolve a bare model name
to `rtvio/data/models/<name>`, the same convention `vggt_reconstruct._load_vggt`
already uses for the VGGT checkpoint - one stable, gitignored location
(`data/models/*.pt` already covered it). Verified: weight now downloads
there and detection behavior is unchanged (still 0 dynamic pixels on the
test frame, as expected from the finding above).

## State of the repo

- `rtvio/src/rtvio/ai_masking.py` - the one substantive change, described
  above. Small, backward compatible (only affects the default/bare-filename
  case; an explicit path or absolute path passed in is untouched).
- `rtvio/data/outputs/masking_test/` - new, gitignored (like all
  `data/outputs/`) - the masked pipeline run's output plus the three
  evidence images for the finding above.
- Nothing else changed. Previous session's commit (`908bced`, "CUDA
  Integration") already covers everything from `HANDOFF_SESSION1.md`.

## Suggested next steps (not started)

1. **Decide whether nadir-aware masking is worth pursuing.** SIH26158's
   actual requirement is "3D Mesh / Point Cloud" - masking is a quality
   nice-to-have, not a spec item (same caveat session 1 noted for
   Gaussian Splatting). If pursued: look for a VisDrone/DOTA-fine-tuned
   YOLO checkpoint (several exist on Ultralytics HQ / HuggingFace) rather
   than training one from scratch.
2. **gsplat training** - still blocked on the RTX 5070 Ti / a CUDA compiler,
   exactly as session 1 left it. Nothing new to try here without that
   hardware.
3. **Test masking on non-nadir footage**, if any becomes available (e.g. an
   oblique/45-degree drone pass, or ground-level shots) - the current
   `yolov8n-seg.pt` may work fine at that viewing angle; this session only
   disproved it for straight-down footage.
4. FBX export still not implemented, still not required (session 1 note
   still stands).
