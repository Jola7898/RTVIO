"""
Batch (not live) reconstruction core, replacing tracking.py's ORB/LK/PnP
tracker + the removed EKF + dense_stereo.py's plane-sweep stereo for the
SIH26158 deliverable. See docs/dev_notes and CHANGELOG.md for why: the
live per-frame pipeline had no global consistency (local 10-frame BA only,
no loop closure) and starved on non-aerial baselines, which is what
produced 3-4x duplicated point clouds; the EKF before it dead-reckoned on
IMU alone whenever GPS was sparse, which is what produced its drift. This
module sidesteps both failure modes by using VGGT (facebookresearch/vggt,
vendored under third_party/vggt/) - a feed-forward transformer that sees a
whole batch of frames at once (real global consistency, no sequential
drift) - and by only ever using GPS to GEOREFERENCE the result (a rigid
fit per window, so a badly-anchored window can't propagate error into
later ones the way a Kalman-filtered dead-reckoning trajectory does).

Also unlike live_pipeline.py, this is intentionally NOT live: SIH26158's
own spec only requires <15 minutes processing for a 10-minute video, a
batch budget, not real-time - see the plan doc. That removes the entire
class of bug the live socket pipeline had (synchronous per-frame cost
starving the TCP reader thread).

Pipeline, per window of ~WINDOW_FRAMES sampled frames:
  1. VGGT forward pass -> per-frame extrinsic/intrinsic + depth + confidence
     (docs/dev_notes has the confirmed API; this is not guesswork).
  2. Unproject confidence-filtered depth to a world-frame point cloud -
     VGGT's own "world" is an arbitrary frame (origin/scale/orientation
     set by the model), not geography.
  3. Georeference: fit a similarity transform (so3.umeyama_alignment,
     verified against synthetic ground truth - see its docstring) between
     VGGT's camera centers and the GPS-derived ENU positions for the same
     frames, then apply it to the window's whole cloud and trajectory.
  4. Accumulate across windows (they share the same GPS-anchored ENU
     frame, so no inter-window loop closure is needed) and run the same
     voxel_downsample + statistical_outlier_removal dense_stereo.py
     already uses once at the end, to clean up window-overlap seams.

Windowing exists only because VGGT has no built-in long-video chunking and
a whole 10-minute flight will not fit in 4GB of VRAM in one forward pass -
see docs/dev_notes for the measured per-window budget on this machine.
"""
import argparse
import csv
import json
import os
import time

import cv2
import numpy as np

from .ai_masking import DynamicMasker
from .dense_stereo import voxel_downsample, statistical_outlier_removal
from .export import export_las, write_mesh_origin_sidecar, export_dsm_raster
from .meshing import build_grid, write_textured_mesh
from .so3 import umeyama_alignment, rigid_from_pose_pair
from .stream.geodesy import latlon_to_enu

# Defaults tuned for a 4GB card - MEASURED, not guessed: on this machine's
# GTX 1650 (4295MB reported total), n=4 frames peaked at 3893MB (fits) in
# 30.4s; n=8 already peaked at 4951MB - over the physical VRAM, silently
# spilling into slow shared system memory (Windows' WDDM allows this
# instead of raising an OOM - see _load_vggt) - and took 66.0s, more than
# 2x n=4's time for 2x the frames. n=16 was killed after several minutes
# without finishing. A bigger GPU can raise WINDOW_FRAMES; this is the one
# knob most worth re-measuring first on different hardware (see
# docs/dev_notes for the full VRAM/timing table and why this pipeline
# ended up needing the aggregator in fp16 with the heads kept in fp32).
WINDOW_FRAMES = 4
WINDOW_OVERLAP = 1          # frames shared between consecutive windows
SAMPLE_FPS = 2.0            # video is sub-sampled to this rate before windowing
DEPTH_CONF_PERCENTILE = 50  # VGGT confidence threshold: keep points above the
                             # window's own median confidence (adaptive, not a
                             # fixed absolute threshold - confidence scale
                             # differs by scene, per the VGGT paper)
MIN_GPS_POINTS_FOR_ALIGN = 3  # umeyama_alignment needs >=3 non-collinear points


def confidence_gate(depth_conf_np, percentile=DEPTH_CONF_PERCENTILE):
    """Adaptive per-window confidence threshold (see DEPTH_CONF_PERCENTILE)
    since VGGT's confidence scale is scene-relative, not an absolute
    probability - a fixed threshold that worked on one scene silently keeps
    ~0 or ~all points on another. This is exactly the kind of silent-zero
    failure the old pipeline's "sparse map stuck at 0 points" bug was (see
    CHANGELOG.md) - the caller logging the kept fraction is how we catch it
    happening again instead of discovering it later. Split out from
    run_window so this can be unit-tested on synthetic arrays without a GPU
    or the checkpoint - see tests/test_vggt_bridge.py.

    The percentile is taken over pixels ABOVE the window's own floor value,
    not the raw array - confirmed on a real bf16 run (this GPU's first; the
    machine that originally wrote this pipeline only ever tested fp16) on a
    low-oblique aerial clip with a lot of flat overcast sky: 84% of ALL
    pixels sat at the EXACT floor confidence value (sky and other
    textureless surfaces have no real signal to estimate depth from). With
    a floor mass that large, percentile=50 of the raw array returns the
    floor itself, so >= against it kept 100% of pixels - the gate silently
    did nothing, and all that near-arbitrary sky depth (each frame guesses
    differently) survived into the cloud. Visually confirmed: this produced
    a thin, cone-shaped point cloud (apex = the bright, low-saturation sky
    pixels VGGT placed at anomalously shallow depth); restricting the
    percentile to only the non-floor pixels resolved it into a coherent
    cluster instead, verified on the same clip. Falls back to the floor
    itself (keep everything) in the one case the previous, simpler version
    was written for: EVERY pixel in the window is truly at the floor (no
    non-floor pixels exist at all) - nothing left to filter by.

    Returns (keep: bool array same shape as depth_conf_np, thresh: float)."""
    floor = depth_conf_np.min()
    above_floor = depth_conf_np[depth_conf_np > floor]
    thresh = np.percentile(above_floor, percentile) if len(above_floor) > 0 else floor
    # >=, not > : thresh is a percentile of values strictly greater than
    # floor (see above_floor's construction), so >= here still correctly
    # excludes every floor-tied pixel in the normal case, while keeping the
    # degenerate all-floor case (thresh==floor) working as "keep everything"
    # rather than "keep nothing" (fp16-ties history this line originally
    # guarded against, still valid - see git history for the pre-bf16 fix).
    keep = depth_conf_np >= max(thresh, 1e-6)
    return keep, thresh


def sample_video_frames(video_path, out_dir, sample_fps=SAMPLE_FPS):
    """Extracts frames at `sample_fps` to `out_dir` as 0-padded JPEGs, and
    returns [(frame_path, video_timestamp_s), ...]. Writing to disk (not
    keeping frames in memory) matches VGGT's own demo scripts (they expect
    an image folder) and means a partially-finished run's frames are
    inspectable on their own - useful for the Day-1 checkpoint."""
    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("could not open video: %s" % video_path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = max(1, round(src_fps / sample_fps))

    frames = []
    idx = 0
    kept = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            t_s = idx / src_fps
            path = os.path.join(out_dir, "frame_%06d.jpg" % kept)
            cv2.imwrite(path, img)
            frames.append((path, t_s))
            kept += 1
        idx += 1
    cap.release()
    print("sample_video_frames: %d source frames (%.1f fps) -> %d sampled at ~%.1f fps"
          % (idx, src_fps, kept, sample_fps))
    return frames


def load_gps_track(path):
    """CSV with columns timestamp_s,lat_deg,lon_deg,alt_m[,accuracy_m] -
    the flight-metadata/telemetry file that accompanies the provided drone
    video (SIH26158's mandatory GPS input), NOT the live phone-stream wire
    protocol (stream/protocol.py) - this module never touches a socket.
    Returns a list of dicts sorted by timestamp."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "t": float(r["timestamp_s"]),
                "lat": float(r["lat_deg"]),
                "lon": float(r["lon_deg"]),
                "alt": float(r["alt_m"]),
            })
    rows.sort(key=lambda r: r["t"])
    return rows


def gps_enu_for_frames(frame_times, gps_track, ref_lat, ref_lon, ref_alt, max_gap_s=2.0):
    """Nearest-neighbour match of each frame's timestamp to the GPS track,
    converted to ENU metres about (ref_lat, ref_lon, ref_alt). Returns a
    parallel list, with None wherever the nearest fix is more than
    `max_gap_s` away - callers must skip those frames for alignment rather
    than trust a stale/extrapolated position."""
    if not gps_track:
        return [None] * len(frame_times)
    ts = np.array([g["t"] for g in gps_track])
    out = []
    for t in frame_times:
        i = int(np.argmin(np.abs(ts - t)))
        if abs(ts[i] - t) > max_gap_s:
            out.append(None)
            continue
        g = gps_track[i]
        e, n, u = latlon_to_enu(g["lat"], g["lon"], g["alt"], ref_lat, ref_lon, ref_alt)
        out.append(np.array([e, n, u]))
    return out


VGGT_CHECKPOINT_ENV = "RTVIO_VGGT_CHECKPOINT"


def _load_vggt(device, dtype):
    """Deferred import: third_party/vggt/ and torch are only needed here,
    so a --help or a unit test that never calls this can run without a
    GPU or a multi-GB download present.

    Loads from a local checkpoint file (RTVIO_VGGT_CHECKPOINT env var, or
    the default download path below) via plain load_state_dict, NOT
    VGGT.from_pretrained's huggingface_hub cache lookup - the checkpoint
    was fetched by curl directly (see docs/dev_notes: huggingface_hub's own
    downloader was too slow on this network, and hit an unrelated
    PowerShell argument-marshalling bug when an attempt was made to run it
    detached), so the hub's on-disk cache layout/hash metadata was never
    populated and from_pretrained would just re-download. This is the
    README's own documented fallback ("model = VGGT(); load_state_dict(...)
    "), not a workaround of unknown correctness."""
    import sys
    import torch
    vggt_root = os.path.join(os.path.dirname(__file__), "..", "..", "third_party", "vggt")
    vggt_root = os.path.abspath(vggt_root)
    if vggt_root not in sys.path:
        sys.path.insert(0, vggt_root)
    from vggt.models.vggt import VGGT

    default_ckpt = os.path.join(os.path.dirname(__file__), "..", "..", "data", "models", "vggt1b_model.pt")
    ckpt_path = os.environ.get(VGGT_CHECKPOINT_ENV, os.path.abspath(default_ckpt))
    # enable_point/enable_track=False: we only ever call aggregator/camera_head/
    # depth_head (see run_window) - point_head and track_head together are
    # ~98M params (~400MB in fp32) this pipeline never uses. Building them
    # anyway just to discard the output would be pure VRAM waste on a card
    # this tight; load_state_dict(strict=False) below is what lets the
    # checkpoint (which DOES have their weights) load into a model that
    # doesn't have those submodules at all.
    model = VGGT(enable_point=False, enable_track=False)
    if os.path.exists(ckpt_path):
        print("loading VGGT-1B from local checkpoint: %s" % ckpt_path)
        # mmap=True, not a plain torch.load: default loading reads the whole
        # 5GB file into a staging buffer AND the final tensors at once
        # (measured ~8GB+ resident and still climbing on this machine's 14GB
        # of system RAM before this fix - a real OOM/thrash risk, not a
        # theoretical one). mmap keeps the file on disk and pages it in
        # instead.
        state_dict = torch.load(ckpt_path, map_location="cpu", mmap=True, weights_only=True)
        model.load_state_dict(state_dict, strict=False)
    else:
        print("local checkpoint not found at %s, falling back to from_pretrained "
              "(will hit the network)" % ckpt_path)
        model.load_state_dict(VGGT.from_pretrained("facebook/VGGT-1B").state_dict(), strict=False)

    if device == "cuda" and dtype == torch.float16:
        # Cast the AGGREGATOR only to fp16, not the whole model. Two things
        # measured on this machine forced this rather than either extreme:
        # - Whole model fp32: ~5GB of weights alone, bigger than this card's
        #   4GB VRAM. model.to(device) "succeeded" anyway (5032MB allocated)
        #   because Windows' WDDM driver silently spills the overflow into
        #   shared system memory over PCIe instead of raising an OOM - not a
        #   crash, ~50-100x slower (a 2-frame forward pass ran 10+ minutes
        #   without finishing instead of the paper's "seconds").
        # - Whole model fp16 (model.half()): fits VRAM (2.5GB) and runs fast,
        #   but depth_head's confidence branch (DPTHead's conf_activation=
        #   "expp1", i.e. exp(x)+1) overflows fp16's ~65504 max and returns
        #   NaN confidence for every pixel on every window - confirmed via
        #   the depth_conf min/max/thresh print in run_window, not guessed.
        # The aggregator (909M of the model's ~1.16B remaining params, see
        # docs/dev_notes for the count) is where nearly all the VRAM and
        # compute goes and has no exponential confidence activation, so
        # halving just that submodule keeps memory low (~2.8GB total) while
        # leaving the small, numerically-sensitive heads in fp32. Under the
        # torch.autocast context run_window already wraps the forward call
        # in, the fp16 aggregator output flowing into the fp32 heads is
        # handled automatically (autocast upcasts an op to fp32 when any
        # operand is fp32) - this is standard mixed-submodule-precision
        # inference, not a hack specific to this codebase.
        model.aggregator = model.aggregator.half()
    model = model.to(device)
    torch.cuda.synchronize() if device == "cuda" else None
    if device == "cuda":
        allocated_mb = torch.cuda.memory_allocated() / 1e6
        total_mb = torch.cuda.get_device_properties(0).total_memory / 1e6
        print("model on GPU: %.0f/%.0f MB VRAM" % (allocated_mb, total_mb))
        if allocated_mb > 0.9 * total_mb:
            print("WARNING: model weights alone use >90%% of VRAM - a real "
                  "window forward pass will likely spill into shared system "
                  "memory (silent, no error, just very slow). Consider "
                  "reducing WINDOW_FRAMES further.")
    model.eval()
    return model


def run_window(model, device, dtype, frame_paths, masker=None):
    """One VGGT forward pass over `frame_paths`. Returns:
      cam_centers_world: (S,3) VGGT-frame camera positions
      cam_R_world:        (S,3,3) VGGT-frame cam-to-world rotations
      points_world:       (N,3) unprojected, confidence-filtered
      colors:              (N,3) uint8 RGB, same length as points_world
      intrinsic:           (S,3,3) per-frame camera intrinsics - only used
                            for the COLMAP export (see export_colmap), not
                            by anything in this window's own reconstruction
    All in VGGT's own arbitrary per-window frame - georeferencing (fitting
    this to GPS) happens in the caller, once per window, not in here.
    """
    import torch
    from vggt.utils.load_fn import load_and_preprocess_images
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    from vggt.utils.geometry import unproject_depth_map_to_point_map, closed_form_inverse_se3

    if masker is not None and masker.enabled:
        # VGGT's own recommendation (README "Detailed Usage"): mask unwanted
        # pixels by zeroing them, rather than filtering the point cloud
        # after the fact - cheaper (one YOLO pass, not two point-cloud
        # passes) and keeps the dynamic-object pixels from ever influencing
        # the aggregator's attention over the *other* frames in the window.
        masked_paths = []
        for i, p in enumerate(frame_paths):
            img = cv2.imread(p)
            mask = masker.get_static_mask(img)
            img[~mask] = 0
            masked_path = p + ".masked.jpg"
            cv2.imwrite(masked_path, img)
            masked_paths.append(masked_path)
        frame_paths = masked_paths

    # load_and_preprocess_images' default mode="crop" fixes width to 518px
    # then center-crops height if THAT overshoots 518 - a no-op for
    # landscape/near-square input (this pipeline's original target: 3840x2160
    # drone footage never triggers it, since height <= width there) but for
    # portrait video (e.g. a phone held vertically - confirmed on a real
    # 1080x1920 test clip) it silently discards ~44% of every frame's
    # vertical extent, foreground and horizon both, leaving VGGT to
    # reconstruct only a squeezed middle strip - this produced a thin,
    # frustum-shaped cloud that looked at first like an insufficient-camera-
    # motion problem but wasn't (confirmed: resampling at a 4x sparser rate,
    # which should increase real inter-frame motion, left the cloud's
    # shape/extent ratio unchanged - ruling that out). mode="pad" preserves
    # every pixel instead (pads the shorter side rather than cropping the
    # longer one) - use it whenever the source is portrait; landscape input's
    # already-verified crop-mode behavior (which never actually crops there)
    # is left untouched.
    sample_h, sample_w = cv2.imread(frame_paths[0]).shape[:2]
    preprocess_mode = "pad" if sample_h > sample_w else "crop"
    images = load_and_preprocess_images(frame_paths, mode=preprocess_mode).to(device)
    with torch.no_grad():
        images_b = images[None]
        # autocast wraps ONLY the aggregator, not the whole forward pass.
        # Measured on this machine: with autocast wrapping camera_head/
        # depth_head too, depth_conf came back NaN for every pixel on every
        # window - EVEN with those heads' own parameters kept in fp32 (see
        # _load_vggt) - because autocast still runs their internal ops
        # (including depth_head's exp()-based confidence activation) in
        # fp16 regardless of the stored parameter dtype; fp16's ~65504 max
        # overflows there. Running the heads as a plain fp32 forward call
        # (their native precision, same as VGGT's own untouched default)
        # outside autocast, on an explicitly-upcast copy of the aggregator's
        # fp16 output, is what actually fixed it - confirmed via the
        # depth_conf min/max print below, not assumed.
        with torch.autocast(device_type=device, dtype=dtype):
            aggregated_tokens_list, ps_idx = model.aggregator(images_b)
        # Aggregator.forward is typed List[Optional[Tensor]] - some entries
        # are legitimately None (confirmed by reading aggregator.py, not
        # guessed after the AttributeError this replaced), so only cast the
        # tensors that exist.
        aggregated_tokens_list = [t.float() if t is not None else None for t in aggregated_tokens_list]
        images_b32 = images_b.float()
        pose_enc = model.camera_head(aggregated_tokens_list)[-1]
        extrinsic, intrinsic = pose_encoding_to_extri_intri(pose_enc, images_b32.shape[-2:])
        depth_map, depth_conf = model.depth_head(aggregated_tokens_list, images_b32, ps_idx)

    extrinsic = extrinsic.squeeze(0).float().cpu().numpy()      # (S,3,4) cam-from-world
    intrinsic = intrinsic.squeeze(0).float().cpu().numpy()      # (S,3,3)
    depth_map_np = depth_map.squeeze(0).float().cpu().numpy()   # (S,H,W,1)
    depth_conf_np = depth_conf.squeeze(0).float().cpu().numpy() # (S,H,W)

    world_points = unproject_depth_map_to_point_map(depth_map_np, extrinsic, intrinsic)  # (S,H,W,3)

    cam_to_world = closed_form_inverse_se3(extrinsic)  # (S,4,4), numpy in -> numpy out
    cam_R_world = cam_to_world[:, :3, :3]
    cam_centers_world = cam_to_world[:, :3, 3]

    keep, thresh = confidence_gate(depth_conf_np, DEPTH_CONF_PERCENTILE)
    print("  depth_conf stats: min=%.4g max=%.4g p%d(thresh)=%.4g"
          % (depth_conf_np.min(), depth_conf_np.max(), DEPTH_CONF_PERCENTILE, thresh))

    imgs_hwc = (images.permute(0, 2, 3, 1).cpu().numpy() * 255.0).astype(np.uint8)  # (S,H,W,3) RGB

    pts = world_points[keep]
    cols = imgs_hwc[keep]
    kept_frac = keep.mean()
    print("  window: %d frames, %d/%d px kept (%.1f%%) after confidence gate"
          % (len(frame_paths), keep.sum(), keep.size, 100 * kept_frac))
    if kept_frac < 0.01:
        print("  WARNING: <1%% of pixels passed the confidence gate - this window's "
              "cloud is likely near-empty, same failure shape as the old pipeline's "
              "'sparse map stuck at 0' bug. Check the frames for blur/low texture.")

    return cam_centers_world, cam_R_world, pts, cols, intrinsic, imgs_hwc


def _collect_colmap_frames(colmap_frames, start, w_paths, cam_R_g, cam_c_g, intrin, imgs_hwc):
    """Records one GLOBAL-frame pose per unique absolute frame index, for
    export_colmap. First occurrence wins: an overlapping frame appears in
    two consecutive windows, and in relative mode (no GPS) the earlier
    window's version has gone through fewer chained transforms - see
    reconstruct()'s RELATIVE MODE branch and so3.rigid_from_pose_pair's
    docstring on accumulated error. Also stashes the actual preprocessed
    image VGGT saw (imgs_hwc[i], not the original video frame) since that's
    what the intrinsics/poses are calibrated against - gsplat trains by
    comparing rendered pixels to exactly these images, so writing out the
    original full-res frame instead would silently mismatch the camera
    model."""
    for i, p in enumerate(w_paths):
        idx = start + i
        if idx not in colmap_frames:
            colmap_frames[idx] = {
                "path": p, "R": cam_R_g[i], "C": cam_c_g[i], "K": intrin[i], "img": imgs_hwc[i],
            }


def export_colmap(out_dir, colmap_frames, pts, cols):
    """Writes a standard COLMAP sparse-reconstruction text dataset (cameras.
    txt/images.txt/points3D.txt + the images/ folder they reference) under
    out_dir/colmap/ - NOT for this pipeline's own use, but as prep for
    Gaussian Splatting training (gsplat) later, per the plan doc's decision
    to prep the data now and defer actual training to a GPU with a working
    CUDA compiler (this machine has none - see the plan doc).

    Deliberately does NOT depend on pycolmap: this codebase has hit
    "no Windows wheel for this Python" for pycolmap/Open3D before (see
    dense_stereo.py, _write_poisson_mesh) and the point of "prep now" was
    to avoid adding more fragile dependencies today. COLMAP's plain-text
    format (https://colmap.github.io/format.html) is simple enough to write
    by hand from data we already have, and is what gsplat's own data loader
    reads directly - no pycolmap needed to consume it either.

    No 2D-3D correspondence tracks are written (empty TRACK per point, and
    no POINTS2D per image) - matches VGGT's own "without bundle adjustment"
    COLMAP export mode (demo_colmap.py's non-BA branch): gsplat's default
    trainer uses points3D.txt only to initialize gaussian positions and
    images.txt/cameras.txt for the actual training views/poses, not the
    correspondence tracks, so this is sufficient for training, just not
    for re-running COLMAP's own bundle adjustment on the result.
    """
    from .georeference import quat_wxyz_from_R

    colmap_dir = os.path.join(out_dir, "colmap", "sparse", "0")
    images_dir = os.path.join(out_dir, "colmap", "images")
    os.makedirs(colmap_dir, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)

    frame_ids = sorted(colmap_frames.keys())
    if not frame_ids:
        print("WARNING: export_colmap called with no frames - skipping")
        return

    cam_lines = ["# Camera list with one line of data per camera:",
                 "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]", "# Number of cameras: %d" % len(frame_ids)]
    img_lines = ["# Image list with two lines of data per image:",
                 "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME",
                 "#   POINTS2D[] as (X, Y, POINT3D_ID)",
                 "# Number of images: %d, mean observations per image: 0" % len(frame_ids)]

    for colmap_id, idx in enumerate(frame_ids, start=1):
        f = colmap_frames[idx]
        K, R_cw, C = f["K"], f["R"], f["C"]
        h, w = f["img"].shape[:2]
        fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
        cam_lines.append("%d PINHOLE %d %d %.6f %.6f %.6f %.6f" % (colmap_id, w, h, fx, fy, cx, cy))

        # COLMAP stores WORLD-TO-CAMERA (the inverse of the cam-to-world R/C
        # this whole pipeline otherwise works in - see run_window/reconstruct).
        R_wc = R_cw.T
        t_wc = -R_wc @ C
        qw, qx, qy, qz = quat_wxyz_from_R(R_wc)
        img_name = "frame_%06d.png" % idx
        img_lines.append("%d %.9f %.9f %.9f %.9f %.9f %.9f %.9f %d %s"
                          % (colmap_id, qw, qx, qy, qz, t_wc[0], t_wc[1], t_wc[2], colmap_id, img_name))
        img_lines.append("")  # empty POINTS2D line - see docstring

        cv2.imwrite(os.path.join(images_dir, img_name), cv2.cvtColor(f["img"], cv2.COLOR_RGB2BGR))

    # Sparse point cloud: the same merged/filtered cloud this run already
    # produced (cloud_raw.ply) - good enough to initialize gaussians from,
    # per gsplat's own docs. No per-point TRACK (see docstring above).
    pts3d_lines = ["# 3D point list with one line of data per point:",
                   "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)",
                   "# Number of points: %d, mean track length: 0" % len(pts)]
    for pid, (p, c) in enumerate(zip(pts, cols), start=1):
        pts3d_lines.append("%d %.6f %.6f %.6f %d %d %d 1.0"
                            % (pid, p[0], p[1], p[2], int(c[0]), int(c[1]), int(c[2])))

    with open(os.path.join(colmap_dir, "cameras.txt"), "w", newline="\n") as fh:
        fh.write("\n".join(cam_lines) + "\n")
    with open(os.path.join(colmap_dir, "images.txt"), "w", newline="\n") as fh:
        fh.write("\n".join(img_lines) + "\n")
    with open(os.path.join(colmap_dir, "points3D.txt"), "w", newline="\n") as fh:
        fh.write("\n".join(pts3d_lines) + "\n")

    print("COLMAP dataset written: %d cameras/images, %d points -> %s"
          % (len(frame_ids), len(pts), os.path.join(out_dir, "colmap")))
    print("  Ready for gsplat once a CUDA-capable machine is available, e.g.:")
    print("  python examples/simple_trainer.py default --data_dir %s --data_factor 1 --result_dir <out>"
          % os.path.join(out_dir, "colmap"))


def reconstruct(video_path, gps_path, out_dir, ref_lat=None, ref_lon=None, ref_alt=None,
                 window_frames=WINDOW_FRAMES, overlap=WINDOW_OVERLAP, sample_fps=SAMPLE_FPS,
                 use_masking=True, cell_size_m=1.0, voxel_size_m=0.3, masking_preset="coco"):
    """Batch reconstruction from a video file + optional GPS CSV. See
    reconstruct_from_recording just below for the phone-session (rtvioapk /
    stream.recorder.SessionRecorder) equivalent - both funnel into
    _reconstruct_core, which doesn't care where frames/GPS came from."""
    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, "frames")
    t_start = time.monotonic()

    frames = sample_video_frames(video_path, frames_dir, sample_fps=sample_fps)
    if len(frames) < 2:
        raise RuntimeError("need at least 2 sampled frames, got %d" % len(frames))
    frame_paths = [p for p, _ in frames]
    frame_times = [t for _, t in frames]
    gps_track = load_gps_track(gps_path) if gps_path else []

    return _reconstruct_core(frame_paths, frame_times, gps_track, out_dir,
                              ref_lat, ref_lon, ref_alt, window_frames, overlap,
                              use_masking, cell_size_m, voxel_size_m, t_start,
                              masking_preset=masking_preset)


def load_gps_track_from_recording(session_dir):
    """Reads gps_data.json (written by stream.recorder.SessionRecorder) directly,
    instead of round-tripping through load_gps_track's hand-made CSV format.
    Field names differ (timestamp/latitude_deg/longitude_deg/altitude_m vs.
    load_gps_track's timestamp_s/lat_deg/lon_deg/alt_m) but mean the same
    thing - mapped here rather than making load_gps_track sniff two formats.
    Returns [] (not an error) if the session has no GPS - same "fall back to
    relative mode" contract as reconstruct()'s gps_path=None."""
    path = os.path.join(session_dir, "gps_data.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = json.load(f)
    out = [{"t": r["timestamp"], "lat": r["latitude_deg"],
             "lon": r["longitude_deg"], "alt": r["altitude_m"]} for r in rows]
    out.sort(key=lambda r: r["t"])
    return out


def reconstruct_from_recording(session_dir, out_dir, ref_lat=None, ref_lon=None, ref_alt=None,
                                window_frames=WINDOW_FRAMES, overlap=WINDOW_OVERLAP,
                                use_masking=True, cell_size_m=1.0, voxel_size_m=0.3,
                                masking_preset="coco"):
    """Batch VGGT reconstruction from a phone-recorded session fixture (see
    stream/recorder.py's SessionRecorder, and live_pipeline.py's --record-only)
    instead of a video file.

    Reads the exact frames actually captured - no re-decode/re-encode through
    a video container (which sample_video_frames' cv2.VideoCapture path would
    do, stacking a second generation of JPEG loss on top per recorder.py's own
    docstring) - and real per-frame timestamps from frame_timestamps.json, not
    an assumed constant fps: a dropped frame is recorded as `None` and skipped
    here, not treated as part of a gap-free sequence (recorder.py's on_frame
    explains why - the app drops frames on purpose when the link congests).

    NOT consumed yet: imu_data.json, camera_intrinsics.json. VGGT
    self-estimates intrinsics/extrinsics, and the georeferencing path
    (umeyama_alignment) only ever uses positions, never orientation - wiring
    either in would need real changes to run_window's VGGT calls, unverified
    to help. Documented future work, not a silent gap."""
    t_start = time.monotonic()
    frame_paths, frame_times = _load_recording_frames(session_dir)
    gps_track = load_gps_track_from_recording(session_dir)
    os.makedirs(out_dir, exist_ok=True)
    return _reconstruct_core(frame_paths, frame_times, gps_track, out_dir,
                              ref_lat, ref_lon, ref_alt, window_frames, overlap,
                              use_masking, cell_size_m, voxel_size_m, t_start,
                              masking_preset=masking_preset)


def _load_recording_frames(session_dir):
    """Reads frame_timestamps.json + frames/*.jpg from a SessionRecorder
    fixture, skipping dropped frames (timestamp `None`, no corresponding
    file - see recorder.py's on_frame). Split out from
    reconstruct_from_recording so this logic (no torch/VGGT import) is
    unit-testable without a GPU or the checkpoint - see
    tests/test_vggt_bridge.py."""
    frames_dir = os.path.join(session_dir, "frames")
    with open(os.path.join(session_dir, "frame_timestamps.json")) as f:
        raw_times = json.load(f)

    frame_paths, frame_times = [], []
    n_dropped = 0
    for idx, t in enumerate(raw_times):
        if t is None:
            n_dropped += 1
            continue
        frame_paths.append(os.path.join(frames_dir, "%06d.jpg" % idx))
        frame_times.append(t)
    if n_dropped:
        print("reconstruct_from_recording: %d of %d frames were dropped during "
              "capture (recorder queue overflow) - proceeding with the %d that "
              "made it to disk" % (n_dropped, len(raw_times), len(frame_paths)))
    if len(frame_paths) < 2:
        raise RuntimeError("need at least 2 recorded frames, got %d" % len(frame_paths))
    return frame_paths, frame_times


def _reconstruct_core(frame_paths, frame_times, gps_track, out_dir, ref_lat, ref_lon, ref_alt,
                       window_frames, overlap, use_masking, cell_size_m, voxel_size_m, t_start,
                       masking_preset="coco"):
    """The shared body of reconstruct()/reconstruct_from_recording(), from
    just after frame/GPS loading onward: windowing, VGGT, georeferencing,
    merging, export. Agnostic to whether frame_paths/frame_times/gps_track
    came from a video file or a phone-recorded session."""
    georeferenced = True
    if ref_lat is None:
        if not gps_track:
            # RELATIVE MODE: no GPS at all, e.g. a phone clip with no
            # telemetry log. Rather than refuse to run, fall back to VGGT's
            # own coordinate frame untouched (no Umeyama fit, since there's
            # nothing to align to) - same spirit as live_pipeline.py's
            # --indoor flag, which the old pipeline's README documents as
            # "the output is not georeferenced" rather than a hard failure.
            # ref_lat/lon/alt below are bookkeeping placeholders only (fed
            # to export_las/export_dsm_raster's UTM math, which needs SOME
            # origin) - they do not claim a real location.
            print("WARNING: no --gps/--ref-lat given - running in RELATIVE mode. "
                  "Output positions/scale come only from VGGT's own estimate, "
                  "are NOT tied to real-world coordinates, and cloud.las's "
                  "UTM location is a meaningless placeholder (0,0). Fine for "
                  "checking reconstruction quality; not for measurement or "
                  "the SIH deliverable, which requires real GPS.")
            georeferenced = False
            ref_lat, ref_lon, ref_alt = 0.0, 0.0, 0.0
        else:
            ref_lat, ref_lon, ref_alt = gps_track[0]["lat"], gps_track[0]["lon"], gps_track[0]["alt"]
    gps_enu = gps_enu_for_frames(frame_times, gps_track, ref_lat, ref_lon, ref_alt)

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" and torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    print("device=%s dtype=%s" % (device, dtype))
    model = _load_vggt(device, dtype)
    masker = None
    if use_masking:
        # coco (default): stock ground-level/oblique detector, unchanged.
        # nadir_aerial: VisDrone-trained checkpoint for straight-down drone
        # footage, where coco detects nothing at all - see
        # DynamicMasker's class docstring and HANDOFF_SESSION2.md/3.md.
        masker = DynamicMasker.for_nadir_aerial() if masking_preset == "nadir_aerial" else DynamicMasker()

    step = window_frames - overlap
    assert step > 0, "WINDOW_OVERLAP must be < window_frames"

    all_pts, all_cols = [], []
    traj_vggt, traj_gps, traj_t = [], [], []   # for the checkpoint trajectory plot
    align_residuals = []
    prev_global_cam_R, prev_global_cam_c = None, None  # relative-mode chaining state
    colmap_frames = {}  # absolute frame idx -> dict(path, R_cam_to_world, C, K) - see export_colmap

    win_starts = list(range(0, len(frame_paths), step))
    window_seconds = []  # per-window wall time - see PERFORMANCE_BENCHMARK.md
    for wi, start in enumerate(win_starts):
        end = min(start + window_frames, len(frame_paths))
        if end - start < 2:
            continue
        w_paths = frame_paths[start:end]
        w_times = frame_times[start:end]
        w_gps = gps_enu[start:end]

        print("window %d/%d: frames [%d:%d]" % (wi + 1, len(win_starts), start, end))
        _w_t0 = time.monotonic()
        cam_c, cam_R, pts, cols, intrin, imgs_hwc = run_window(model, device, dtype, w_paths, masker)
        window_seconds.append(time.monotonic() - _w_t0)

        if not georeferenced:
            # RELATIVE MODE, no GPS: chain this window onto the previous
            # one via their shared overlapping frame(s), instead of leaving
            # every window floating in its own disconnected coordinate
            # frame (confirmed on a real test that identity-passthrough
            # produces exactly that - see docs/dev_notes). so3.
            # rigid_from_pose_pair uses ONE shared camera's full pose
            # (rotation AND position = 6 constraints, exactly enough for a
            # rigid transform) rather than umeyama_alignment's >=3-point
            # requirement, so this works even at the default
            # WINDOW_OVERLAP=1 - no need to shrink the window step (and
            # therefore blow up the window count) just to get enough shared
            # points. See rigid_from_pose_pair's docstring for the one real
            # weakness (unverified per-window scale drift).
            if wi == 0:
                R_t, t_t = np.eye(3), np.zeros(3)
            else:
                R_t, t_t = rigid_from_pose_pair(
                    prev_global_cam_R[-overlap], prev_global_cam_c[-overlap],
                    cam_R[0], cam_c[0])
            cam_c_g = (R_t @ cam_c.T).T + t_t
            cam_R_g = np.einsum("ij,njk->nik", R_t, cam_R)
            pts_g = (R_t @ pts.T).T + t_t
            prev_global_cam_R, prev_global_cam_c = cam_R_g, cam_c_g

            all_pts.append(pts_g)
            all_cols.append(cols)
            for ct, cc in zip(w_times, cam_c_g):
                traj_t.append(ct)
                traj_vggt.append(cc)
                traj_gps.append([np.nan, np.nan, np.nan])
            _collect_colmap_frames(colmap_frames, start, w_paths, cam_R_g, cam_c_g, intrin, imgs_hwc)
            continue

        anchors_src = [c for c, g in zip(cam_c, w_gps) if g is not None]
        anchors_dst = [g for g in w_gps if g is not None]
        if len(anchors_src) >= MIN_GPS_POINTS_FOR_ALIGN:
            s, R, t = umeyama_alignment(np.array(anchors_src), np.array(anchors_dst), with_scale=True)
            pred = s * (R @ np.array(anchors_src).T).T + t
            resid = np.linalg.norm(pred - np.array(anchors_dst), axis=1)
            align_residuals.append((wi, float(resid.mean()), float(resid.max()), len(anchors_src)))
            print("  GPS alignment: %d anchor frames, residual mean=%.2fm max=%.2fm"
                  % (len(anchors_src), resid.mean(), resid.max()))
        elif wi > 0:
            # Not enough GPS in this window - carry the previous window's
            # transform forward rather than leaving the cloud unanchored.
            # Flagged explicitly so a long GPS-sparse stretch is visible in
            # the report, the same spirit as live_pipeline's old "vision
            # pose updates: N/M frames" warning.
            print("  WARNING: only %d GPS anchor(s) in this window (<%d needed) - "
                  "reusing the previous window's alignment" % (len(anchors_src), MIN_GPS_POINTS_FOR_ALIGN))
        else:
            raise RuntimeError(
                "first window has fewer than %d GPS fixes within range - cannot "
                "establish an initial georeference. Check --ref-lat/lon/alt and "
                "the GPS track's timestamp alignment." % MIN_GPS_POINTS_FOR_ALIGN)

        cam_c_g = s * (R @ cam_c.T).T + t
        cam_R_g = np.einsum("ij,njk->nik", R, cam_R)   # scale doesn't touch rotation
        pts_g = s * (R @ pts.T).T + t

        all_pts.append(pts_g)
        all_cols.append(cols)
        for ct, cc, gg in zip(w_times, cam_c_g, w_gps):
            traj_t.append(ct)
            traj_vggt.append(cc)
            traj_gps.append(gg if gg is not None else [np.nan, np.nan, np.nan])
        _collect_colmap_frames(colmap_frames, start, w_paths, cam_R_g, cam_c_g, intrin, imgs_hwc)

    raw_pts = np.vstack(all_pts).astype(np.float64)
    raw_cols = np.vstack(all_cols).astype(np.float64)
    # voxel_size_m default (0.3) is dense_stereo.py's aerial-scale constant,
    # inherited as a starting point, not re-derived for this pipeline. On
    # the ~7m-wide kitchen test scene it collapsed 2.9M points to 136 (a
    # 0.3m cell is a large fraction of the whole scene) - the same class of
    # "aerial constant silently wrong at a smaller scale" issue CHANGELOG.md
    # already documents for MIN_BASELINE_M. Exposed as a parameter rather
    # than hardcoded so it can be set to match the actual scene scale
    # (small indoor test vs. real tens-to-hundreds-of-metres drone footage)
    # instead of silently over- or under-collapsing either one.
    #
    # RELATIVE mode makes even a hand-picked "real" metres value meaningless:
    # confirmed on real drone footage (see docs/dev_notes) that VGGT's own
    # unanchored scale guess for a high-altitude aerial shot came out as a
    # ~1.2-unit-wide scene - not metres, just VGGT's internal units, which
    # nothing here forces to correspond to real-world size when there's no
    # GPS to fix the scale. voxel_size_m=0.3 against a 1.2-unit scene is the
    # same "cell is a large fraction of the whole scene" collapse again
    # (456876 raw points -> 37). So in relative mode, derive the voxel size
    # from the cloud's OWN extent instead of trusting a metres value that
    # has no defined relationship to VGGT's made-up units.
    voxel_size_eff = voxel_size_m
    if not georeferenced:
        extent = raw_pts.max(axis=0) - raw_pts.min(axis=0)
        voxel_size_eff = max(float(extent.max()) / 150.0, 1e-6)
        print("RELATIVE mode: scene extent in VGGT's own units is %s -> using "
              "adaptive voxel size %.4g instead of the nominal %.3g (which has no "
              "defined relationship to these units)" % (extent, voxel_size_eff, voxel_size_m))
    pts_v, cols_v = voxel_downsample(raw_pts, raw_cols, voxel_size=voxel_size_eff)
    pts, cols = statistical_outlier_removal(pts_v, cols_v)
    print("merged cloud: %d raw -> %d after voxel downsample -> %d after outlier "
          "removal (from %d windows)" % (len(raw_pts), len(pts_v), len(pts), len(win_starts)))
    if len(pts) < 0.1 * len(pts_v):
        # Debugging aid for exactly the collapse the Day-1 checkpoint hit:
        # if separate windows' clouds don't spatially overlap (e.g. from a
        # bad per-window GPS scale), voxel-downsampling each window's dense
        # patch into a handful of cells leaves those cells looking like
        # isolated outliers to the k=8 neighbor test, since their real
        # neighbors (the rest of their own window's now-merged patch) are
        # gone - SOR then strips out most of the cloud, not because the
        # points are wrong, but because voxel+SOR back-to-back assumes the
        # voxelized cloud is still locally dense, which multiple thin,
        # disjoint window patches are not.
        print("WARNING: outlier removal dropped >90%% of the voxel-downsampled cloud "
              "(%d -> %d) - check whether per-window clouds actually overlap in space "
              "(a bad GPS alignment scale, or windows covering non-overlapping parts "
              "of the scene, both look like this)." % (len(pts_v), len(pts)))

    # Same "metres are meaningless without GPS" fix as voxel_size_eff above,
    # applied to the 2.5D mesh's grid cell size - finer than the point voxel
    # size (extent/300 vs /150) since the mesh benefits from more resolution
    # than the point merge does.
    cell_size_eff = cell_size_m if georeferenced else max(
        float((raw_pts.max(axis=0) - raw_pts.min(axis=0)).max()) / 300.0, 1e-6)

    video_span_s = (frame_times[-1] - frame_times[0]) if len(frame_times) > 1 else 0.0
    _write_outputs(out_dir, pts, cols, ref_lat, ref_lon, ref_alt, cell_size_eff,
                    traj_t, traj_vggt, traj_gps, align_residuals,
                    wall_s=time.monotonic() - t_start, n_frames=len(frame_paths),
                    georeferenced=georeferenced, window_seconds=window_seconds,
                    video_span_s=video_span_s)
    export_colmap(out_dir, colmap_frames, pts, cols)
    return pts, cols


def _write_outputs(out_dir, pts, cols, ref_lat, ref_lon, ref_alt, cell_size_m,
                    traj_t, traj_vggt, traj_gps, align_residuals, wall_s, n_frames,
                    georeferenced=True, window_seconds=None, video_span_s=0.0):
    """Everything the Day-1/Day-2 plan checkpoints ask for: the point
    cloud/mesh itself, plus the verification artifacts (trajectory plot,
    alignment residuals, timing) so results can be checked, not just
    trusted. See ~/.claude/plans/here-i-have-a-fancy-treasure.md."""
    epsg_out = export_las(pts, cols, ref_lat, ref_lon, ref_alt, os.path.join(out_dir, "cloud.las"))

    grid = build_grid(pts, cols, cell_size_m=cell_size_m)
    mesh_info = write_textured_mesh(grid, os.path.join(out_dir, "mesh_2p5d.obj"))
    write_mesh_origin_sidecar(ref_lat, ref_lon, ref_alt, epsg_out, os.path.join(out_dir, "mesh_origin.json"))
    export_dsm_raster(grid, ref_lat, ref_lon, ref_alt, os.path.join(out_dir, "dsm.png"))

    _write_poisson_mesh(pts, cols, out_dir)

    # newline="": plain "w" mode on Windows silently rewrites every \n to
    # \r\n, which is invisible in a text editor but breaks strict PLY
    # readers that search for the exact byte sequence "end_header\n" (e.g.
    # SuperSplat's readPly - confirmed by inspecting this file's raw bytes
    # after a real load failure, not guessed).
    with open(os.path.join(out_dir, "cloud_raw.ply"), "w", newline="") as f:
        f.write("ply\nformat ascii 1.0\nelement vertex %d\n" % len(pts))
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
        for p, c in zip(pts, cols):
            f.write("%f %f %f %d %d %d\n" % (p[0], p[1], p[2], int(c[0]), int(c[1]), int(c[2])))

    _write_trajectory_plot(out_dir, traj_t, traj_vggt, traj_gps)

    # The old version of this line divided wall_s by a flat 900s regardless
    # of how long the input actually was - meaningful only when the test
    # clip happens to be ~10 minutes long, which none of this pipeline's
    # real test clips have been (see PERFORMANCE_BENCHMARK.md). Scale by the
    # input's own span instead, and project what a real 10-minute video
    # would cost at the same per-window rate - that's the number SIH26158's
    # <15min target is actually about.
    if video_span_s > 0:
        projected_10min_s = wall_s * (600.0 / video_span_s)
        budget_line = ("Wall time: %.1f s for %.1f s of input video (%.2fx realtime). "
                        "Projected for a 10-minute video at this rate: %.0f s "
                        "(%.2fx SIH26158's 15-minute budget)"
                        % (wall_s, video_span_s, video_span_s / wall_s if wall_s > 0 else 0.0,
                           projected_10min_s, projected_10min_s / (15 * 60)))
    else:
        budget_line = "Wall time: %.1f s (input video span unknown, cannot project to 10min)" % wall_s

    report = [
        "# VGGT batch reconstruction - checkpoint report", "",
        "Georeferenced: %s" % ("YES (anchored to provided GPS)" if georeferenced else
                               "NO - relative mode, no GPS given. Positions/scale are "
                               "VGGT's own estimate only; cloud.las's UTM location is a "
                               "placeholder, not real. Fine for checking reconstruction "
                               "quality, not for the SIH deliverable."),
        "Frames processed: %d" % n_frames,
        budget_line,
    ]
    if window_seconds:
        report.append(
            "Per-window time: min=%.1fs mean=%.1fs max=%.1fs (n=%d windows)"
            % (min(window_seconds), sum(window_seconds) / len(window_seconds),
               max(window_seconds), len(window_seconds)))
    report += [
        "Merged cloud points: %d" % len(pts),
        "2.5D mesh: %d vertices, %d faces, %.1f%% cell completeness"
        % (mesh_info["n_vertices"], mesh_info["n_faces"], 100 * mesh_info["completeness"]),
        "", "## Per-window GPS alignment residuals (mean/max metres, n anchor frames)", "",
    ]
    for wi, mean_r, max_r, n in align_residuals:
        report.append("- window %d: mean=%.2fm max=%.2fm (%d anchors)" % (wi, mean_r, max_r, n))
    with open(os.path.join(out_dir, "CHECKPOINT_REPORT.md"), "w") as f:
        f.write("\n".join(report))
    print("\n".join(report))


def _write_poisson_mesh(pts, cols, out_dir):
    """pymeshlab, not Open3D - verified during planning that Open3D has no
    wheel for this machine's Python (see the plan doc); pymeshlab wraps the
    same screened-Poisson filter MeshLab/Open3D both use, and does have one.
    Wrapped in try/except: this is the "gives real facades" upgrade over
    the always-available 2.5D mesh, not the thing that should block a run
    if the meshlab filter set on this machine differs from expected."""
    try:
        import pymeshlab
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(vertex_matrix=pts, v_color_matrix=np.hstack(
            [cols / 255.0, np.ones((len(cols), 1))])))
        ms.compute_normal_for_point_clouds(k=16)
        ms.generate_surface_reconstruction_screened_poisson(depth=9)
        ms.save_current_mesh(os.path.join(out_dir, "mesh_poisson.obj"))
        ms.save_current_mesh(os.path.join(out_dir, "mesh_poisson.ply"))
        _write_gltf(os.path.join(out_dir, "mesh_poisson.obj"), os.path.join(out_dir, "mesh_poisson.glb"))
        print("Poisson mesh written: mesh_poisson.obj/.ply/.glb")
    except Exception as e:
        print("WARNING: Poisson meshing failed (%s) - falling back to the 2.5D mesh only. "
              "This does not block LAS/2.5D-OBJ output above." % e)


def _write_gltf(obj_path, glb_path):
    try:
        import trimesh
        mesh = trimesh.load(obj_path, force="mesh")
        mesh.export(glb_path)
    except Exception as e:
        print("WARNING: glTF export failed (%s)" % e)


def _write_trajectory_plot(out_dir, traj_t, traj_vggt, traj_gps):
    """The Day-1 checkpoint's 'does the GPS anchoring actually work' plot -
    see the plan doc. Wrapped in try/except so a missing matplotlib
    doesn't block the actual reconstruction output above it."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        vggt_xy = np.array(traj_vggt)[:, :2]
        gps_xy = np.array(traj_gps)[:, :2]
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.plot(vggt_xy[:, 0], vggt_xy[:, 1], "b.-", label="VGGT (GPS-anchored)", markersize=3)
        valid = ~np.isnan(gps_xy[:, 0])
        ax.plot(gps_xy[valid, 0], gps_xy[valid, 1], "r.", label="raw GPS", markersize=5)
        ax.set_xlabel("East (m)")
        ax.set_ylabel("North (m)")
        ax.set_title("Reconstructed trajectory vs. raw GPS")
        ax.legend()
        ax.axis("equal")
        fig.savefig(os.path.join(out_dir, "trajectory_check.png"), dpi=150)
        plt.close(fig)
        print("trajectory_check.png written")
    except Exception as e:
        print("WARNING: trajectory plot failed (%s)" % e)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="single-pass drone/phone video file")
    src.add_argument("--from-recording", metavar="DIR",
                      help="a session fixture directory from live_pipeline.py "
                           "--record-only (or --record) instead of a video file - "
                           "see reconstruct_from_recording. --gps is ignored with "
                           "this option; GPS comes from the recording's own "
                           "gps_data.json.")
    ap.add_argument("--gps", default=None, help="CSV: timestamp_s,lat_deg,lon_deg,alt_m "
                                                 "(--video only)")
    ap.add_argument("--ref-lat", type=float, default=None)
    ap.add_argument("--ref-lon", type=float, default=None)
    ap.add_argument("--ref-alt", type=float, default=None)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--window-frames", type=int, default=WINDOW_FRAMES)
    ap.add_argument("--overlap", type=int, default=WINDOW_OVERLAP)
    ap.add_argument("--sample-fps", type=float, default=SAMPLE_FPS,
                     help="--video only - a recording already has real per-frame "
                          "timestamps, nothing to resample")
    ap.add_argument("--no-masking", action="store_true", help="skip YOLO dynamic-object masking")
    ap.add_argument("--masking-preset", choices=["coco", "nadir_aerial"], default="coco",
                     help="coco (default): ground-level/oblique footage. nadir_aerial: "
                          "straight-down drone footage, where coco detects nothing at all "
                          "(see DynamicMasker's class docstring) - uses a VisDrone-trained "
                          "checkpoint instead, downloaded to data/models/ on first use.")
    ap.add_argument("--cell-size-m", type=float, default=1.0)
    ap.add_argument("--voxel-size-m", type=float, default=0.3,
                     help="dense-cloud merge voxel size - shrink this for small/indoor "
                          "scenes (default 0.3 is aerial-scale, see reconstruct()'s docstring)")
    args = ap.parse_args()

    common = dict(
        ref_lat=args.ref_lat, ref_lon=args.ref_lon, ref_alt=args.ref_alt,
        window_frames=args.window_frames, overlap=args.overlap,
        use_masking=not args.no_masking, masking_preset=args.masking_preset,
        cell_size_m=args.cell_size_m,
        voxel_size_m=args.voxel_size_m,
    )
    if args.from_recording:
        reconstruct_from_recording(args.from_recording, args.out, **common)
    else:
        reconstruct(args.video, args.gps, args.out, sample_fps=args.sample_fps, **common)


if __name__ == "__main__":
    main()
