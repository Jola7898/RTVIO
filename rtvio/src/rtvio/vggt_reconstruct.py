"""
Batch VGGT reconstruction: a phone recording (or a video file) in,
cloud_raw.ply + mesh_poisson.ply out.

    python -m rtvio.vggt_reconstruct --from-recording data/sessions/<id> --out <dir>
    python -m rtvio.vggt_reconstruct --video clip.mp4 --out <dir>

Built for the indoor, vision-only case: no GPS (unreliable indoors), no IMU
(drifts), every captured frame used, and the GPU kept busy the whole time.

WHAT HAPPENS
 1. Frames. Every frame of the recording (--frame-stride 1). Portrait phone
    frames are rotated to landscape before VGGT sees them: VGGT's input is
    518 px wide, so a 9:16 portrait frame either loses 44% of its height to
    the "crop" preprocess or, with "pad", spends 43% of its tokens on white
    padding. Rotated, it becomes the 518x294 shape VGGT's own demo uses for
    16:9 video - all pixels, no padding, and ~3x less global-attention work
    per frame. The rotation is undone on the outputs (see _output_frame).
    A recording that carries a lens calibration with distortion
    (camera_intrinsics.json - a calibrated drone camera; --intrinsics for a
    video file) is first remapped to an ideal pinhole camera, frame by frame
    (camera_model.py). VGGT cannot represent a wide-angle/fisheye lens:
    without this, straight walls bend and right angles open up.
 2. Windows. Frames go through VGGT in overlapping windows sized to fill
    the free VRAM ("auto"). Bigger windows mean more frames solved jointly
    (VGGT's global attention is its multi-view consistency) and fewer
    window-to-window seams. Decoding/resizing of the next window happens on
    a thread pool while the GPU runs the current one.
 3. Alignment. Each window comes out in its own frame and scale; it is
    mapped onto the previous one by a robust Sim(3) fitted to the dense
    per-pixel correspondences of their shared frames (fusion.robust_sim3).
    Scale is corrected at every seam, which the previous single-camera rigid
    chaining could not do.
 4. Fusion. Confidence-gated, edge-filtered pixels are averaged into voxels
    about one pixel footprint wide (fusion.VoxelAccumulator); voxels seen by
    fewer than --min-views distinct frames are dropped, then a statistical
    outlier filter runs. -> cloud_raw.ply (binary, with normals oriented
    toward the cameras that saw each point).
 5. Surface. Screened Poisson on that oriented cloud, trimmed back to where
    there is data, coloured from the cloud. -> mesh_poisson.ply
 6. Optional (--extras): LAS, OBJ/GLB, COLMAP export for gsplat.

GPS is off unless --gps-mode global: then every frame with a fix becomes an
anchor for ONE similarity fit of the whole trajectory (hundreds of anchors
instead of the old 3-4 per window - see docs/dev_notes/HANDOFF_SESSION3.md item 3 on why
per-window fits could not reach the accuracy target).
"""
import argparse
import csv
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from . import camera_model
from .ingest import sharpness_score
from .stream.geodesy import latlon_to_enu

VGGT_SIZE = 518
WINDOW_FRAMES = "auto"
WINDOW_OVERLAP = 8          # shared frames between consecutive windows (the Sim(3) correspondences)
SAMPLE_FPS = 0.0            # --video only: 0 = every frame of the source
DEPTH_CONF_PERCENTILE = 50  # keep pixels above this percentile of the window's above-floor confidence
EDGE_REL_THRESH = 0.04      # per-pixel relative depth jump treated as a depth edge (fusion.continuity_mask)
MIN_GPS_POINTS_FOR_ALIGN = 3
MIN_ALIGN_CORRESPONDENCES = 2000

# Window sizing, from a sweep on this project's RTX 5070 Ti (16 GB, bf16
# aggregator, real 4:3 phone frames = 518x392 input, 1036 tokens/frame):
#
#   frames/window   16    32    48    64    80    96   112   128
#   ms per frame    99   129   170   207   246   329   346   899 <- spilled
#   peak VRAM GB   7.5   8.1   8.8   9.4  10.0  10.7  11.3  12.3
#
# The card is at 100% utilisation at every size - VGGT is compute-bound -
# but per-frame cost grows with window size (global attention is quadratic
# in frames), while consistency improves with it. 64 is the cap: every frame
# is solved jointly with 63 others, at ~0.24 s per new frame with the
# default overlap. Past ~110 frames WDDM starts spilling to shared system
# memory (no OOM is raised - it just runs 3x slower, the 128 column).
# Memory = a fixed DPT-head chunk cost + a per-frame token cost:
AUTO_MB_PER_1K_TOKENS_PER_FRAME = 39.0     # (8403 - 4585) MB / (112 - 16) frames at 1036 tokens
AUTO_DPT_MB_PER_FRAME_MPIX = 1230.0        # ~4 GB for a 16-frame chunk of 518x392
AUTO_VRAM_HEADROOM_MB = 1500.0
AUTO_MAX_WINDOW = 64
AUTO_MIN_WINDOW = 12
# bf16 heads: the fp16 NaN problem (see _load_vggt) is an fp16 range issue;
# bf16 has fp32's exponent range. Same sweep: bf16 vs fp32 heads differ by a
# median 0.03% in depth, confidence all finite.
HEADS_IN_BF16 = True
DPT_FRAMES_CHUNK = 16

VGGT_CHECKPOINT_ENV = "RTVIO_VGGT_CHECKPOINT"

# Rotated-camera coordinates -> original portrait-camera coordinates, for a
# frame rotated with cv2.ROTATE_90_CLOCKWISE: rotated pixel (u', v') =
# (H0-1-v, u), so the rotated x axis is the original -y and rotated y is +x.
R_UNROTATE = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
# OpenCV camera axes (x right, y down, z forward) -> Y-up (x right, y up,
# z toward the viewer), what three.js/MeshLab/Blender-glTF expect.
CV_TO_YUP = np.diag([1.0, -1.0, -1.0])


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------- inputs --

def confidence_gate(depth_conf_np, percentile=DEPTH_CONF_PERCENTILE):
    """Adaptive per-window confidence threshold: the percentile is taken over
    pixels ABOVE the window's own floor value, not the raw array.

    Confirmed on a real bf16 run on a low-oblique aerial clip with a lot of
    flat overcast sky: 84% of ALL pixels sat at the exact floor confidence.
    With a floor mass that large, percentile 50 of the raw array IS the
    floor, so >= against it kept every pixel - the gate silently did nothing
    and the near-arbitrary sky depth survived into the cloud as a thin cone.
    Falls back to the floor itself (keep everything) when every pixel is at
    the floor. fusion.confidence_keep is the torch twin used on the GPU;
    tests/test_fusion.py checks the two agree.

    Returns (keep: bool array same shape as depth_conf_np, thresh: float)."""
    floor = depth_conf_np.min()
    above_floor = depth_conf_np[depth_conf_np > floor]
    thresh = np.percentile(above_floor, percentile) if len(above_floor) > 0 else floor
    keep = depth_conf_np >= max(thresh, 1e-6)
    return keep, thresh


def sample_video_frames(video_path, out_dir, sample_fps=SAMPLE_FPS):
    """Extracts frames to out_dir as JPEGs (quality 95) and returns
    [(frame_path, video_timestamp_s), ...]. sample_fps <= 0 keeps every
    frame of the source."""
    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("could not open video: %s" % video_path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = 1 if not sample_fps or sample_fps <= 0 else max(1, round(src_fps / sample_fps))
    frames, idx, kept = [], 0, 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            path = os.path.join(out_dir, "frame_%06d.jpg" % kept)
            cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            frames.append((path, idx / src_fps))
            kept += 1
        idx += 1
    cap.release()
    log("sample_video_frames: %d source frames (%.1f fps) -> %d kept (stride %d)"
        % (idx, src_fps, kept, stride))
    return frames


def load_gps_track(path):
    """CSV with columns timestamp_s,lat_deg,lon_deg,alt_m[,accuracy_m] - the
    telemetry file that accompanies a provided drone video. Sorted by time."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"t": float(r["timestamp_s"]), "lat": float(r["lat_deg"]),
                         "lon": float(r["lon_deg"]), "alt": float(r["alt_m"])})
    rows.sort(key=lambda r: r["t"])
    return rows


def gps_enu_for_frames(frame_times, gps_track, ref_lat, ref_lon, ref_alt, max_gap_s=2.0):
    """Nearest GPS fix for each frame time, in ENU metres about the reference;
    None where the nearest fix is more than max_gap_s away."""
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
        out.append(np.array(latlon_to_enu(g["lat"], g["lon"], g["alt"], ref_lat, ref_lon, ref_alt)))
    return out


def load_gps_track_from_recording(session_dir):
    """gps_data.json of a recorded session (stream.recorder.SessionRecorder or
    rtvio.studio), mapped onto load_gps_track's field names. [] if absent."""
    path = os.path.join(session_dir, "gps_data.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = json.load(f)
    out = [{"t": r["timestamp"], "lat": r["latitude_deg"],
            "lon": r["longitude_deg"], "alt": r["altitude_m"]} for r in rows]
    out.sort(key=lambda r: r["t"])
    return out


def _load_recording_frames(session_dir):
    """frame_timestamps.json + frames/NNNNNN.jpg of a recorded session,
    skipping dropped frames (timestamp None, no file on disk)."""
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
        log("reconstruct_from_recording: %d of %d frames were dropped during capture - "
            "proceeding with the %d that made it to disk" % (n_dropped, len(raw_times), len(frame_paths)))
    if len(frame_paths) < 2:
        raise RuntimeError("need at least 2 recorded frames, got %d" % len(frame_paths))
    return frame_paths, frame_times


# ---------------------------------------------------------------- model --

def _load_vggt(device):
    """VGGT-1B with only the camera and depth heads (the point and track
    heads are ~98M params this pipeline never calls), from a local checkpoint
    (RTVIO_VGGT_CHECKPOINT or data/models/vggt1b_model.pt) via mmap - a plain
    torch.load stages the whole 5 GB file in RAM on top of the tensors.

    Precision: the aggregator (~909M of the remaining params, and nearly all
    of the compute) is cast to bf16 on sm_80+ and fp16 below that, halving
    its VRAM. On fp16 the heads must stay fp32 and outside autocast -
    depth_head's exp()-based confidence overflows fp16's 65504 and returns
    NaN for every pixel (found on the 4 GB GTX 1650 this was first built
    on). bf16 has fp32's exponent range, so on bf16 hardware the heads run
    under autocast like VGGT's own demo (HEADS_IN_BF16).

    Returns (model, dtype)."""
    import torch
    vggt_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "third_party", "vggt"))
    if vggt_root not in sys.path:
        sys.path.insert(0, vggt_root)
    from vggt.models.vggt import VGGT

    if device == "cuda":
        dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    else:
        dtype = torch.float32
    default_ckpt = os.path.join(os.path.dirname(__file__), "..", "..", "data", "models", "vggt1b_model.pt")
    ckpt_path = os.environ.get(VGGT_CHECKPOINT_ENV, os.path.abspath(default_ckpt))
    model = VGGT(enable_point=False, enable_track=False)
    if os.path.exists(ckpt_path):
        log("loading VGGT-1B from %s" % ckpt_path)
        state_dict = torch.load(ckpt_path, map_location="cpu", mmap=True, weights_only=True)
        model.load_state_dict(state_dict, strict=False)
    else:
        log("local checkpoint not found at %s, falling back to from_pretrained (network)" % ckpt_path)
        model.load_state_dict(VGGT.from_pretrained("facebook/VGGT-1B").state_dict(), strict=False)
    if dtype != torch.float32:
        model.aggregator = model.aggregator.to(dtype)
    model = model.to(device).eval()
    if device == "cuda":
        torch.cuda.synchronize()
        log("model on GPU: %.0f MB (%s aggregator), %.0f MB free of %.0f MB"
            % (torch.cuda.memory_allocated() / 1e6, str(dtype).replace("torch.", ""),
               torch.cuda.mem_get_info()[0] / 1e6, torch.cuda.get_device_properties(0).total_memory / 1e6))
    return model, dtype


def _vggt_forward(model, imgs, device, dtype):
    """imgs (S,3,H,W) float in [0,1] on device -> cam-from-world extrinsics
    (S,3,4), intrinsics (S,3,3), depth (S,H,W), confidence (S,H,W), float32."""
    import torch
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    x = imgs[None]
    autocast = device == "cuda" and dtype != torch.float32
    with torch.no_grad():
        with torch.autocast(device_type=device, dtype=dtype, enabled=autocast):
            toks, ps = model.aggregator(x)
        heads_autocast = autocast and dtype == torch.bfloat16 and HEADS_IN_BF16
        if not heads_autocast:
            toks = [t.float() if t is not None else None for t in toks]
        with torch.autocast(device_type=device, dtype=dtype, enabled=heads_autocast):
            pose_enc = model.camera_head(toks)[-1]
            depth, conf = model.depth_head(toks, x, ps, frames_chunk_size=DPT_FRAMES_CHUNK)
        del toks
        extr, intr = pose_encoding_to_extri_intri(pose_enc.float(), x.shape[-2:])
    return extr[0].float(), intr[0].float(), depth[0, ..., 0].float(), conf[0].float()


def auto_window_frames(tokens_per_frame, pixels_per_frame, free_mb):
    """Largest window that fits the free VRAM (see the sweep table above),
    capped at AUTO_MAX_WINDOW where per-frame cost stops being worth it."""
    per_frame = AUTO_MB_PER_1K_TOKENS_PER_FRAME * tokens_per_frame / 1000.0
    fixed = AUTO_DPT_MB_PER_FRAME_MPIX * DPT_FRAMES_CHUNK * pixels_per_frame / 1e6
    n = int((free_mb - fixed - AUTO_VRAM_HEADROOM_MB) / per_frame)
    return max(AUTO_MIN_WINDOW, min(AUTO_MAX_WINDOW, n))


# --------------------------------------------------------------- frames --

class FrameLoader:
    """Decodes, rotates and resizes frames to VGGT's input on a thread pool,
    ahead of the GPU. Resizing with INTER_AREA (an averaging filter) rather
    than VGGT's PIL bicubic: a 720/1080 px frame is being shrunk ~1.4-2x
    and area averaging is the alias-free way to do that."""

    def __init__(self, paths, workers=8, masker=None, camera=None, undistort_balance=0.0):
        self.paths = paths
        self.masker = masker
        self._mask_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.futures = {}
        self.blur = {}          # frame index -> ingest.sharpness_score, filled in as _load runs
        probe = cv2.imread(paths[0], cv2.IMREAD_COLOR)
        if probe is None:
            raise RuntimeError("cannot read %s" % paths[0])
        h0, w0 = probe.shape[:2]
        self.src_size = (w0, h0)
        # Lens undistortion, in the frames' native orientation (before the
        # portrait rotation below). The remap tables are read-only, so the
        # worker threads share them.
        self.undistort = None
        self.undistort_info = None
        if camera is not None:
            q = camera_model.scaled(camera, w0, h0)
            if q is None:
                log("WARNING: the lens calibration is %sx%s but the frames are %dx%d (a different aspect) "
                    "- frames NOT undistorted" % (camera.get("width"), camera.get("height"), w0, h0))
            elif camera_model.has_distortion(q):
                m1, m2, K_new = camera_model.undistort_maps(q, undistort_balance)
                self.undistort = (m1, m2)
                lens = camera_model.fov_deg(q)
                ph, pv = camera_model.pinhole_fov_deg(K_new, w0, h0)
                self.undistort_info = {
                    "model": q["model"], "source": q.get("source"), "width": w0, "height": h0,
                    "lens_hfov": lens[0], "lens_vfov": lens[1], "pinhole_hfov": ph, "pinhole_vfov": pv,
                    "fx": float(K_new[0, 0]), "fy": float(K_new[1, 1]),
                    "cx": float(K_new[0, 2]), "cy": float(K_new[1, 2]),
                }
        self.rotated = h0 > w0
        rh, rw = (w0, h0) if self.rotated else (h0, w0)
        self.W = VGGT_SIZE
        self.H_resized = int(round(rh * VGGT_SIZE / rw / 14.0) * 14)
        self.H = min(self.H_resized, VGGT_SIZE)

    def _load(self, i):
        img = cv2.imread(self.paths[i], cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("cannot read %s" % self.paths[i])
        if self.undistort is not None:
            img = cv2.remap(img, self.undistort[0], self.undistort[1], cv2.INTER_LINEAR)
        self.blur[i] = float(sharpness_score(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)))
        if self.rotated:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        static = None
        if self.masker is not None and self.masker.enabled:
            with self._mask_lock:                       # ultralytics predictors are not thread-safe
                static = self.masker.get_static_mask(img)
        img = cv2.resize(img, (self.W, self.H_resized), interpolation=cv2.INTER_AREA)
        top = (self.H_resized - self.H) // 2
        img = img[top:top + self.H]
        rgb = np.ascontiguousarray(img[:, :, ::-1])
        if static is not None:
            static = cv2.resize(static.astype(np.uint8), (self.W, self.H_resized),
                                interpolation=cv2.INTER_NEAREST)[top:top + self.H].astype(bool)
            rgb[~static] = 0
        return rgb, static

    def prefetch(self, idxs):
        for i in idxs:
            if i not in self.futures:
                self.futures[i] = self.pool.submit(self._load, i)

    def get(self, idxs):
        idxs = list(idxs)
        self.prefetch(idxs)
        return [self.futures[i].result() for i in idxs]

    def release_before(self, i):
        for k in [k for k in self.futures if k < i]:
            del self.futures[k]

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class Progress:
    """progress.json for rtvio.studio's job view. Written atomically; on
    Windows os.replace fails while a reader has the file open, so retry."""

    def __init__(self, path):
        self.path = path
        self.t0 = time.monotonic()
        self.state = {}

    def update(self, **kw):
        self.state.update(kw)
        self.state["elapsed_s"] = round(time.monotonic() - self.t0, 1)
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            for _ in range(20):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    time.sleep(0.05)
        except OSError:
            pass


# ------------------------------------------------------------ the core --

def reconstruct(video_path, gps_path, out_dir, sample_fps=SAMPLE_FPS, progress_path=None, **opts):
    """Video file (+ optional GPS CSV) -> reconstruction. See reconstruct_frames."""
    os.makedirs(out_dir, exist_ok=True)
    progress = Progress(progress_path)
    progress.update(stage="frames", detail="extracting video frames", fraction=0.0)
    frames = sample_video_frames(video_path, os.path.join(out_dir, "frames"), sample_fps=sample_fps)
    if len(frames) < 2:
        raise RuntimeError("need at least 2 sampled frames, got %d" % len(frames))
    gps_track = load_gps_track(gps_path) if gps_path else []
    if gps_path and opts.get("gps_mode") is None:
        opts["gps_mode"] = "global"
    opts["camera"] = _lens_profile(opts.pop("intrinsics", None), opts.pop("undistort", True))
    return reconstruct_frames([p for p, _ in frames], [t for _, t in frames], gps_track,
                              out_dir, progress=progress, **opts)


def reconstruct_from_recording(session_dir, out_dir, progress_path=None, **opts):
    """A recorded phone session (rtvio.studio, live_pipeline --record-only, or
    the app's offline RECORD LOCALLY) -> reconstruction. Reads the JPEGs
    exactly as the phone encoded them and their real capture timestamps."""
    os.makedirs(out_dir, exist_ok=True)
    frame_paths, frame_times = _load_recording_frames(session_dir)
    gps_track = load_gps_track_from_recording(session_dir)
    # Outdoor vs indoor is a phone-side setting (Settings -> Outdoor mode):
    # an indoor/no-fix session's gps_data.json is empty, an outdoor one has
    # real fixes. Mirrors the --video + --gps auto-default above, so nobody
    # has to remember --gps-mode global for a session that already recorded
    # GPS - only an explicit --gps-mode overrides this.
    if gps_track and opts.get("gps_mode") is None:
        opts["gps_mode"] = "global"
    # The session's own lens calibration first (a drone take recorded after
    # its camera was calibrated); --intrinsics only for one that has none.
    opts["camera"] = _lens_profile(os.path.join(session_dir, "camera_intrinsics.json"),
                                   opts.pop("undistort", True), fallback=opts.pop("intrinsics", None))
    return reconstruct_frames(frame_paths, frame_times, gps_track, out_dir,
                              progress=Progress(progress_path), **opts)


def _lens_profile(path, undistort=True, fallback=None):
    """The lens calibration to undistort frames with, or None: the first of
    path / fallback that has a size and non-zero distortion. A phone's
    Camera2 intrinsics (no size, ~zero distortion) never qualify - there is
    nothing to straighten."""
    if not undistort:
        return None
    for p in (path, fallback):
        prof = camera_model.load_profile(p) if p else None
        if prof is not None and prof.get("width") and prof.get("height") and camera_model.has_distortion(prof):
            log("lens calibration: %s (%s)" % (p, prof.get("source") or prof["model"]))
            return prof
    return None


def _plan_next(start, n, window, overlap):
    """[start, end) of the window starting at `start`. A tail shorter than
    half a window is absorbed into this one instead of becoming a sliver
    window with too little context to be reconstructed well."""
    end = min(start + window, n)
    if n - end < max(overlap + 2, window // 2) and n - start <= int(window * 1.25):
        end = n
    return end


class _ReconState:
    """Everything one reconstruction accumulates across windows. Shared,
    mutated-in-place state so [_process_window] can be called either from
    reconstruct_frames's batch loop (frames all on disk already) or from
    vggt_live's online loop (frames still arriving) with identical results
    for identical input - the window-to-window algorithm itself does not
    know or care which caller it is."""

    def __init__(self):
        self.acc = None
        self.voxel = None
        self.prev = None            # previous window: start/end + global point maps
        self.poses = {}             # frame index -> (R_cw_g, C_g, K) in the global VGGT frame
        self.seams = []             # per-window alignment diagnostics
        self.blur = []              # per-window motion-blur summary (or None), parallel to windows processed
        self.wi = 0                 # windows successfully processed (excludes OOM retries)


def _process_window(model, device, dtype, loader, masker, start, end,
                    conf_percentile, edge_threshold, voxel_factor, state,
                    prefetch_range=None, viz=None):
    """Runs VGGT on loader's frames [start:end), aligns the result onto
    state.prev via a robust Sim(3) fit over their shared frames, and fuses
    it into state.acc - the one window-to-window algorithm every caller
    (batch or live) shares. Mutates state.acc/voxel/prev/poses/seams on
    success; raises torch.OutOfMemoryError untouched on failure (state is
    only mutated after the forward pass succeeds, so a caller can shrink
    its window size and retry the same `start` safely).

    prefetch_range: indices to kick off decoding on loader's thread pool
    while the GPU runs this window - batch mode's look-ahead onto frames
    already known to exist on disk. Live mode passes None: the next
    window's frames may not have arrived yet.

    viz: optional recon_viz.ReconViz - if given, the window's newly-fused
    points and a representative frame are pushed for a live preview (see
    recon_viz.py). Purely an observer; never affects state or the result.

    Returns (kept_frac, thresh, seam, dt, blur_stats) for the caller to log.
    """
    import torch
    from .fusion import unproject, continuity_mask, pixel_normals, confidence_keep, robust_sim3
    from .fusion import VoxelAccumulator
    from .so3 import rigid_from_pose_pair

    idxs = list(range(start, end))
    t_w = time.monotonic()
    loaded = loader.get(idxs)
    if prefetch_range is not None:
        loader.prefetch(prefetch_range)

    rgb = torch.from_numpy(np.stack([im for im, _ in loaded])).to(device, non_blocking=True)
    imgs = rgb.permute(0, 3, 1, 2).float().div_(255.0)
    extr, intr, depth, conf = _vggt_forward(model, imgs, device, dtype)   # may raise OOM

    # ---- per-pixel geometry, all on the GPU
    R_cw = extr[:, :3, :3].transpose(1, 2)
    C = -(R_cw @ extr[:, :3, 3:4]).squeeze(-1)
    P = unproject(depth, intr, R_cw, C)
    keep, thresh = confidence_keep(conf, conf_percentile)
    valid = keep & continuity_mask(depth, edge_threshold) & (depth > 0) & torch.isfinite(P).all(-1)
    if masker is not None:
        static = torch.from_numpy(np.stack([m if m is not None else np.ones((loader.H, loader.W), bool)
                                            for _, m in loaded])).to(device)
        valid &= static
    normals, nvalid = pixel_normals(P, C)
    valid &= nvalid

    # ---- align onto the previous window
    prev = state.prev
    if prev is None:
        s, R, t = 1.0, torch.eye(3, device=device), torch.zeros(3, device=device)
        seam = None
    else:
        k = prev["end"] - start                       # shared frames
        off = start - prev["start"]
        both = valid[:k] & prev["valid"][off:off + k]
        n_corr = int(both.sum())
        if n_corr >= MIN_ALIGN_CORRESPONDENCES:
            s_, R_, t_, info = robust_sim3(P[:k][both], prev["P"][off:off + k][both],
                                           prev["depth"][off:off + k][both])
            s, R, t = float(s_), R_.float(), t_.float()
            seam = dict(window=state.wi, method="dense-sim3", shared_frames=k, **info, scale=s)
        else:
            # Too little confident overlap (e.g. a whip pan between the
            # shared frames): fall back to chaining through one shared
            # camera, with the scale from the two windows' depth ratio.
            Rg, Cg = prev["R_cw"][off + k - 1].cpu().numpy(), prev["C"][off + k - 1].cpu().numpy()
            dl = depth[k - 1][valid[k - 1]]
            dg = prev["depth"][off + k - 1][prev["valid"][off + k - 1]]
            s = float(dg.median() / dl.median()) if dl.numel() and dg.numel() else 1.0
            R_np, t_np = rigid_from_pose_pair(Rg, Cg, R_cw[k - 1].cpu().numpy(), s * C[k - 1].cpu().numpy())
            R = torch.tensor(R_np, dtype=torch.float32, device=device)
            t = torch.tensor(t_np, dtype=torch.float32, device=device)
            seam = dict(window=state.wi, method="single-camera fallback", shared_frames=k,
                        n=n_corr, scale=s, inlier_frac=None, median_rel_residual=None)
            log("  WARNING: only %d confident shared pixels - fell back to single-camera chaining" % n_corr)
        state.seams.append(seam)

    P_g = s * torch.einsum("ij,shwj->shwi", R, P) + t
    C_g = s * (C @ R.T) + t
    R_cw_g = torch.einsum("ij,sjk->sik", R, R_cw)
    depth_g = depth * s
    normals_g = torch.einsum("ij,shwj->shwi", R, normals)

    if state.acc is None:
        med_depth = float(depth_g[valid].median()) if bool(valid.any()) else 1.0
        fx = float(intr[:, 0, 0].median())
        state.voxel = voxel_factor * med_depth / fx
        state.acc = VoxelAccumulator(state.voxel)
        log("fusion voxel: %.4g (median depth %.3g / fx %.1f x %.2f)" % (state.voxel, med_depth, fx, voxel_factor))

    j0 = (prev["end"] - start) if prev is not None else 0
    sel = valid[j0:]
    fid = torch.arange(start + j0, end, device=device).view(-1, 1, 1).expand_as(sel)
    state.acc.add(P_g[j0:][sel], rgb[j0:][sel], normals_g[j0:][sel], fid[sel])

    if viz is not None:
        new_pts = P_g[j0:][sel]
        if new_pts.shape[0] > 0:
            viz.push_points(new_pts.detach().cpu().numpy(), rgb[j0:][sel].detach().cpu().numpy())
        # A representative frame from this window (already VGGT-resized) - just
        # for "what is currently being looked at", not a deliverable asset.
        prev_rgb = loaded[-1][0]
        ok, jpg = cv2.imencode(".jpg", prev_rgb[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            viz.push_frame(jpg.tobytes())

    C_np, R_np, K_np = C_g.cpu().numpy(), R_cw_g.cpu().numpy(), intr.cpu().numpy()
    for j, idx in enumerate(idxs):
        if idx not in state.poses:
            state.poses[idx] = (R_np[j], C_np[j], K_np[j])

    state.prev = {"start": start, "end": end, "P": P_g, "valid": valid, "depth": depth_g,
                  "R_cw": R_cw_g, "C": C_g}
    if device == "cuda":
        torch.cuda.synchronize()
    state.wi += 1

    blur_vals = {i: loader.blur[i] for i in idxs if i in loader.blur}
    blur_stats = None
    if blur_vals:
        worst_i = min(blur_vals, key=blur_vals.get)
        blur_stats = {"median": float(np.median(list(blur_vals.values()))),
                      "worst": float(blur_vals[worst_i]), "worst_frame": int(worst_i)}
    state.blur.append(blur_stats)

    kept = float(valid.float().mean())
    dt = time.monotonic() - t_w
    return kept, thresh, seam, dt, blur_stats


def _finalize_and_write(state, frame_paths, frame_times, loader, out_dir, progress,
                        gps_mode, gps_track, ref_lat, ref_lon, ref_alt,
                        min_views, make_mesh, poisson_depth, extras, cell_size_m,
                        window, overlap, vggt_s, wall_fn, conf_percentile, frame_stride,
                        peak_mb, gpu_name=None, viz=None):
    """The shared tail of a reconstruction, once every window has been
    processed: fuse the accumulated voxels into a point cloud, georeference
    if requested, mesh, export, and write the report/trajectory plot. Used
    by both reconstruct_frames (batch) and vggt_live (online) so a live and
    a from-recording run of the same footage produce the same files.

    wall_fn: zero-arg callable returning elapsed wall-clock seconds so far
    (reconstruct_frames measures from before model load; vggt_live measures
    from stream start) - kept as a callback rather than a plain float so the
    caller decides what "wall time" means for its own report line.
    """
    from .surface import statistical_outlier_mask, write_ply_points, poisson_mesh, write_ply_mesh

    n = len(frame_paths)
    progress.update(stage="fusing", detail="fusing %d windows" % state.wi, fraction=0.86, eta_s=None)
    pts, cols, nrm, views = state.acc.finish(min_views=min_views)
    n_vox = state.acc.n_voxels
    keep_sor = statistical_outlier_mask(pts, k=12, std_ratio=2.0)
    pts, cols, nrm, views = pts[keep_sor], cols[keep_sor], nrm[keep_sor], views[keep_sor]
    log("fused cloud: %d pixels in -> %d voxels -> %d seen by >= %d frames -> %d after outlier filter"
        % (state.acc.n_points_in, n_vox, int(keep_sor.size), min_views, len(pts)))

    # ---- output frame
    frame_ids = sorted(state.poses)
    cam_C = np.array([state.poses[i][1] for i in frame_ids])
    cam_R = np.array([state.poses[i][0] for i in frame_ids])
    georef = None
    if gps_mode == "global" and gps_track:
        georef = _georeference_global(cam_C, [frame_times[i] for i in frame_ids], gps_track,
                                      ref_lat, ref_lon, ref_alt)
    if georef is not None:
        s_g, R_g, t_g = georef["s"], georef["R"], georef["t"]
        pts = s_g * pts @ R_g.T + t_g
        nrm = (nrm @ R_g.T).astype(np.float32)
        cam_C = s_g * cam_C @ R_g.T + t_g
        cam_R = np.einsum("ij,njk->nik", R_g, cam_R)
        scale_out = s_g
        frame_desc = "ENU metres about %.7f, %.7f, %.1f m" % (georef["ref"])
    else:
        M = CV_TO_YUP @ (R_UNROTATE if loader.rotated else np.eye(3))
        pts = pts @ M.T
        nrm = (nrm @ M.T).astype(np.float32)
        cam_C = cam_C @ M.T
        cam_R = np.einsum("ij,njk->nik", M, cam_R)
        scale_out = 1.0
        frame_desc = ("relative (VGGT units, not metres): first camera at the origin, "
                      "Y up, looking down -Z")

    write_ply_points(os.path.join(out_dir, "cloud_raw.ply"), pts, cols, normals=nrm,
                     scalars={"views": views.astype(np.float32)})
    log("cloud_raw.ply written: %d points" % len(pts))
    _write_cameras_json(out_dir, frame_ids, frame_times, cam_R, cam_C, state.poses, frame_desc)
    progress.update(stage="meshing", detail="Poisson surface (depth %d)" % poisson_depth,
                    fraction=0.9, points=len(pts), outputs=["cloud_raw.ply"])

    mesh_info = None
    if make_mesh and len(pts) > 1000:
        vox_out = state.voxel * scale_out
        extent = float(np.linalg.norm(np.percentile(pts, 99, axis=0) - np.percentile(pts, 1, axis=0)))
        cell = 1.1 * extent / (2 ** poisson_depth)
        trim = max(3.0 * vox_out, 2.0 * cell)
        try:
            verts, faces, vcols, vn, mesh_info = poisson_mesh(pts, nrm, cols, depth=poisson_depth,
                                                              trim_dist=trim, log=log)
            write_ply_mesh(os.path.join(out_dir, "mesh_poisson.ply"), verts, faces, cols=vcols, normals=vn)
            log("mesh_poisson.ply written: %d vertices, %d faces" % (len(verts), len(faces)))
            if extras:
                _write_mesh_extras(out_dir, verts, faces, vcols)
        except Exception as e:                                     # noqa: BLE001
            log("WARNING: Poisson meshing failed (%s: %s) - cloud_raw.ply is still complete"
                % (type(e).__name__, e))
            mesh_info = {"error": str(e)}

    if extras:
        progress.update(stage="extras", detail="LAS / COLMAP export", fraction=0.97)
        _write_extras(out_dir, pts, cols, georef, frame_ids, frame_paths, cam_R, cam_C, state.poses, loader,
                      cell_size_m)

    wall = wall_fn()
    span = frame_times[-1] - frame_times[0] if n > 1 else 0.0
    summary = _write_report(out_dir, n=n, window=window, overlap=overlap, windows=state.wi, vggt_s=vggt_s, wall_s=wall,
                            span_s=span, peak_mb=peak_mb, n_in=state.acc.n_points_in, n_vox=n_vox, n_pts=len(pts),
                            voxel=state.voxel * scale_out, seams=state.seams, blur=state.blur, mesh_info=mesh_info,
                            georef=georef, frame_desc=frame_desc, rotated=loader.rotated, min_views=min_views,
                            conf_percentile=conf_percentile, frame_stride=frame_stride, gpu=gpu_name,
                            camera=_camera_diagnostic(state, frame_ids, loader))
    _write_trajectory_plot(out_dir, cam_C, georef is not None)
    outputs = [f for f in ("cloud_raw.ply", "mesh_poisson.ply") if os.path.exists(os.path.join(out_dir, f))]
    progress.update(stage="done", detail="done", fraction=1.0, eta_s=0, points=len(pts),
                    faces=(mesh_info or {}).get("faces"), outputs=outputs, vggt_seconds=round(vggt_s, 1))
    log("done in %.1f s (VGGT %.1f s for %d frames = %.1f frames/s)" % (wall, vggt_s, n, n / max(vggt_s, 1e-6)))
    if viz is not None:
        viz.push_report(summary)
    return pts, cols


def reconstruct_frames(frame_paths, frame_times, gps_track, out_dir, progress=None,
                       window_frames=WINDOW_FRAMES, overlap=WINDOW_OVERLAP, frame_stride=1,
                       max_frames=0, conf_percentile=DEPTH_CONF_PERCENTILE,
                       edge_threshold=EDGE_REL_THRESH, voxel_factor=1.0, min_views=2,
                       poisson_depth=10, make_mesh=True, gps_mode=None,
                       ref_lat=None, ref_lon=None, ref_alt=None,
                       use_masking=False, masking_preset="coco", extras=False,
                       cell_size_m=1.0, viz=None, camera=None, undistort_balance=0.0):
    import torch

    progress = progress or Progress(None)
    t_start = time.monotonic()
    os.makedirs(out_dir, exist_ok=True)

    if frame_stride > 1:
        frame_paths, frame_times = frame_paths[::frame_stride], frame_times[::frame_stride]
    if max_frames and len(frame_paths) > max_frames:
        frame_paths, frame_times = frame_paths[:max_frames], frame_times[:max_frames]
    n = len(frame_paths)
    if n < 2:
        raise RuntimeError("need at least 2 frames, got %d" % n)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        # Not cudnn.benchmark: it re-autotunes every new tensor shape, and the
        # sweep measured 7-11 s stalls for each - the last (shorter) window
        # and the DPT head's remainder chunk are new shapes every run.
        torch.backends.cudnn.benchmark = False
    else:
        log("WARNING: no CUDA device - running VGGT on the CPU will be extremely slow")

    progress.update(stage="loading", detail="loading VGGT-1B", fraction=0.0, frames=n)
    model, dtype = _load_vggt(device)

    masker = None
    if use_masking:
        from .ai_masking import DynamicMasker
        masker = DynamicMasker.for_nadir_aerial() if masking_preset == "nadir_aerial" else DynamicMasker()
    loader = FrameLoader(frame_paths, masker=masker, camera=camera, undistort_balance=undistort_balance)
    tokens = (loader.H // 14) * (loader.W // 14)
    log("input: %d frames %dx%d%s -> VGGT %dx%d (%d tokens/frame)"
        % (n, loader.src_size[0], loader.src_size[1],
           ", portrait -> rotated to landscape" if loader.rotated else "",
           loader.W, loader.H, tokens))
    u = loader.undistort_info
    if u:
        log("lens: %s, %.0f x %.0f deg -> every frame undistorted to a pinhole %.0f x %.0f deg "
            "(fx %.1f px at %dx%d) before VGGT" % (u["model"], u["lens_hfov"], u["lens_vfov"],
                                                   u["pinhole_hfov"], u["pinhole_vfov"], u["fx"],
                                                   u["width"], u["height"]))

    if window_frames in (None, "auto", "0", 0):
        free_mb = torch.cuda.mem_get_info()[0] / 1e6 if device == "cuda" else 8000
        window = auto_window_frames(tokens, loader.H * loader.W, free_mb)
        log("auto window: %d frames per VGGT pass (%.0f MB VRAM free)" % (window, free_mb))
    else:
        window = int(window_frames)
    window = max(2, min(window, n))
    overlap = max(1, min(int(overlap), window // 2))
    est_windows = 1 if n <= window else 1 + math.ceil((n - window) / (window - overlap))

    state = _ReconState()
    window_times = []
    frames_done = 0
    start = 0
    loader.prefetch(range(0, _plan_next(0, n, window, overlap)))

    while True:
        end = _plan_next(start, n, window, overlap)
        next_start = end - overlap if end < n else None
        prefetch_range = (range(next_start, _plan_next(next_start, n, window, overlap))
                          if next_start is not None else None)
        try:
            kept, thresh, seam, dt, blur_stats = _process_window(
                model, device, dtype, loader, masker, start, end,
                conf_percentile, edge_threshold, voxel_factor, state, prefetch_range, viz=viz)
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            if window <= AUTO_MIN_WINDOW:
                raise
            window = max(AUTO_MIN_WINDOW, int(window * 0.75))
            overlap = min(overlap, window // 2)
            log("  out of VRAM - retrying with %d-frame windows" % window)
            continue

        window_times.append((end - start, dt))
        frames_done = end
        rate = sum(f for f, _ in window_times) / max(sum(d for _, d in window_times), 1e-6)
        eta = (n - frames_done) / max(rate, 1e-6) + 0.15 * (time.monotonic() - t_start)
        log("window %d: frames [%d:%d) %.2fs (%.1f frames/s), %.0f%% px kept, conf>=%.3g%s"
            % (state.wi, start, end, dt, (end - start) / dt, 100 * kept, thresh,
               "" if seam is None else ", seam s=%.4f resid=%s" % (
                   seam["scale"], "%.3f" % seam["median_rel_residual"] if seam["median_rel_residual"] is not None else "-")))
        progress.update(stage="vggt", window=state.wi, windows=max(est_windows, state.wi),
                        frames_done=frames_done, fraction=0.85 * frames_done / n,
                        detail="VGGT window %d/%d · %.1f frames/s" % (state.wi, max(est_windows, state.wi), rate),
                        eta_s=round(eta), vggt_fps=round(rate, 2), window_frames=window)
        if viz is not None:
            viz.push_window({
                "window": state.wi, "windows_est": max(est_windows, state.wi),
                "frames_done": frames_done, "frames_total": n, "fps": round(rate, 2),
                "kept_frac": kept, "conf_thresh": thresh,
                "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
                "seam": ({"method": seam["method"], "scale": seam["scale"],
                          "median_rel_residual": seam.get("median_rel_residual")} if seam is not None else None),
                "blur": blur_stats,
            })
        if next_start is None:
            break
        loader.release_before(next_start)
        start = next_start

    loader.close()
    peak_mb = torch.cuda.max_memory_allocated() / 1e6 if device == "cuda" else 0.0
    vggt_s = sum(d for _, d in window_times)
    gpu_name = torch.cuda.get_device_name(0) if device == "cuda" else None
    del state.prev
    if device == "cuda":
        torch.cuda.empty_cache()

    return _finalize_and_write(
        state, frame_paths, frame_times, loader, out_dir, progress,
        gps_mode, gps_track, ref_lat, ref_lon, ref_alt,
        min_views, make_mesh, poisson_depth, extras, cell_size_m,
        window, overlap, vggt_s, lambda: time.monotonic() - t_start, conf_percentile, frame_stride,
        peak_mb, gpu_name=gpu_name, viz=viz)


# -------------------------------------------------------------- outputs --

def _georeference_global(cam_C, times, gps_track, ref_lat, ref_lon, ref_alt):
    """One robust similarity from the whole chained trajectory to GPS ENU.
    None (stay relative) when there are too few fixes, or they span too
    little ground for a scale/heading to be observable."""
    import torch
    from .fusion import robust_sim3
    if ref_lat is None:
        ref_lat, ref_lon, ref_alt = gps_track[0]["lat"], gps_track[0]["lon"], gps_track[0]["alt"]
    enu = gps_enu_for_frames(times, gps_track, ref_lat, ref_lon, ref_alt, max_gap_s=1.0)
    src = np.array([c for c, g in zip(cam_C, enu) if g is not None])
    dst = np.array([g for g in enu if g is not None])
    if len(src) < MIN_GPS_POINTS_FOR_ALIGN:
        log("GPS: only %d frames have a fix within 1 s - staying in relative mode" % len(src))
        return None
    spread = float(np.linalg.norm(dst.max(0) - dst.min(0)))
    if spread < 5.0:
        log("GPS: fixes span only %.1f m - too little to observe scale/heading through "
            "GPS noise; staying in relative mode" % spread)
        return None
    s, R, t, info = robust_sim3(torch.tensor(src), torch.tensor(dst))
    s, R, t = float(s), R.numpy(), t.numpy()
    resid = np.linalg.norm(s * src @ R.T + t - dst, axis=1)
    log("GPS: global Sim(3) over %d anchors: residual median %.2f m, 90%% %.2f m"
        % (len(src), np.median(resid), np.percentile(resid, 90)))
    return {"s": s, "R": R, "t": t, "ref": (ref_lat, ref_lon, ref_alt), "n": len(src),
            "resid_median": float(np.median(resid)), "resid_p90": float(np.percentile(resid, 90))}


def _camera_diagnostic(state, frame_ids, loader):
    """VGGT's own focal estimate, next to the lens calibration if there is
    one. It is the number that says whether VGGT's idea of the field of
    view - and so every angle in the model - matches the real camera."""
    fx518 = float(np.median([state.poses[i][2][0, 0] for i in frame_ids]))
    src_w = loader.src_size[1] if loader.rotated else loader.src_size[0]   # what VGGT's 518 px spans
    fx = fx518 * src_w / loader.W
    out = {"vggt_fx_px": round(fx, 1), "width": src_w,
           "vggt_hfov_deg": round(math.degrees(2 * math.atan(src_w / 2.0 / fx)), 1),
           "undistort": loader.undistort_info}
    if loader.undistort_info:
        known = loader.undistort_info["fy" if loader.rotated else "fx"]
        out["calibrated_fx_px"] = round(known, 1)
        out["ratio"] = round(fx / known, 3)
    return out


def _write_cameras_json(out_dir, frame_ids, frame_times, cam_R, cam_C, poses, frame_desc):
    with open(os.path.join(out_dir, "cameras.json"), "w", newline="\n") as f:
        json.dump({
            "frame": frame_desc,
            "convention": "R_cam_to_world maps OpenCV camera axes into this frame; K is for the "
                          "518-px VGGT input image",
            "centers": np.round(cam_C, 5).tolist(),
            "frames": [{"index": int(i), "time": round(float(frame_times[i]), 4),
                        "R_cam_to_world": np.round(cam_R[j], 6).tolist(),
                        "center": np.round(cam_C[j], 5).tolist(),
                        "K": np.round(poses[i][2], 3).tolist()}
                       for j, i in enumerate(frame_ids)],
        }, f)


def _write_mesh_extras(out_dir, verts, faces, vcols):
    try:
        import trimesh
        m = trimesh.Trimesh(vertices=verts, faces=faces,
                            vertex_colors=np.hstack([vcols, np.full((len(vcols), 1), 255, np.uint8)]),
                            process=False)
        m.export(os.path.join(out_dir, "mesh_poisson.obj"))
        m.export(os.path.join(out_dir, "mesh_poisson.glb"))
    except Exception as e:                                          # noqa: BLE001
        log("WARNING: OBJ/GLB export failed (%s)" % e)


def _write_extras(out_dir, pts, cols, georef, frame_ids, frame_paths, cam_R, cam_C, poses, loader,
                  cell_size_m):
    from .export import export_las, write_mesh_origin_sidecar, export_dsm_raster
    ref = georef["ref"] if georef else (0.0, 0.0, 0.0)
    try:
        epsg = export_las(pts, cols.astype(np.float64), *ref, os.path.join(out_dir, "cloud.las"))
        write_mesh_origin_sidecar(*ref, epsg, os.path.join(out_dir, "mesh_origin.json"))
        if georef:
            from .meshing import build_grid, write_textured_mesh
            grid = build_grid(pts, cols.astype(np.float64), cell_size_m=cell_size_m)
            write_textured_mesh(grid, os.path.join(out_dir, "mesh_2p5d.obj"))
            export_dsm_raster(grid, *ref, os.path.join(out_dir, "dsm.png"))
    except Exception as e:                                          # noqa: BLE001
        log("WARNING: LAS/DSM export failed (%s)" % e)
    try:
        export_colmap(out_dir, frame_ids, cam_R, cam_C, poses, loader, pts, cols)
    except Exception as e:                                          # noqa: BLE001
        log("WARNING: COLMAP export failed (%s)" % e)


def export_colmap(out_dir, frame_ids, cam_R, cam_C, poses, loader, pts, cols, max_points=500_000):
    """Plain-text COLMAP sparse model (cameras/images/points3D.txt + images/)
    for Gaussian-splatting training with gsplat. The images are the exact
    518-px frames VGGT saw (what the intrinsics are calibrated against), and
    the poses are in the same output frame as cloud_raw.ply. No 2D-3D tracks:
    gsplat only uses points3D to initialise gaussians."""
    from .georeference import quat_wxyz_from_R
    sparse = os.path.join(out_dir, "colmap", "sparse", "0")
    images_dir = os.path.join(out_dir, "colmap", "images")
    os.makedirs(sparse, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)
    cam_lines, img_lines = [], []
    for cid, (j, i) in enumerate(zip(range(len(frame_ids)), frame_ids), start=1):
        rgb, _ = loader._load(i)
        name = "frame_%06d.jpg" % i
        cv2.imwrite(os.path.join(images_dir, name), rgb[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 95])
        K = poses[i][2]
        cam_lines.append("%d PINHOLE %d %d %.6f %.6f %.6f %.6f"
                         % (cid, loader.W, loader.H, K[0, 0], K[1, 1], K[0, 2], K[1, 2]))
        R_wc = cam_R[j].T
        t_wc = -R_wc @ cam_C[j]
        qw, qx, qy, qz = quat_wxyz_from_R(R_wc)
        img_lines += ["%d %.9f %.9f %.9f %.9f %.9f %.9f %.9f %d %s"
                      % (cid, qw, qx, qy, qz, t_wc[0], t_wc[1], t_wc[2], cid, name), ""]
    sel = np.random.default_rng(0).choice(len(pts), size=min(max_points, len(pts)), replace=False)
    pt_lines = ["%d %.6f %.6f %.6f %d %d %d 1.0" % (k + 1, *pts[i], *cols[i]) for k, i in enumerate(sel)]
    for name, lines in (("cameras.txt", cam_lines), ("images.txt", img_lines), ("points3D.txt", pt_lines)):
        with open(os.path.join(sparse, name), "w", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    log("COLMAP export: %d images, %d points -> %s" % (len(frame_ids), len(sel), os.path.join(out_dir, "colmap")))


def _write_report(out_dir, **r):
    seams = r["seams"]
    scales = [s["scale"] for s in seams]
    resid = [s["median_rel_residual"] for s in seams if s.get("median_rel_residual") is not None]
    fallbacks = sum(1 for s in seams if s["method"] != "dense-sim3")
    blur_all = [b for b in r.get("blur", []) if b is not None]
    gpu = r.get("gpu")
    lines = [
        "# VGGT reconstruction report", "",
        "Output frame: %s" % r["frame_desc"],
        "Frames: %d (stride %d)%s" % (r["n"], r["frame_stride"], ", portrait rotated to landscape for VGGT" if r["rotated"] else ""),
        "Windows: %d x up to %d frames, %d shared between neighbours" % (r["windows"], r["window"], r["overlap"]),
        "VGGT: %.1f s = %.1f frames/s, peak VRAM %.0f MB%s" % (r["vggt_s"], r["n"] / max(r["vggt_s"], 1e-6), r["peak_mb"],
                                                                 " (%s)" % gpu if gpu else ""),
        "Wall time: %.1f s for %.1f s of capture (%.2fx real time)" % (r["wall_s"], r["span_s"], r["span_s"] / max(r["wall_s"], 1e-6)),
        "", "## Cloud (cloud_raw.ply)", "",
        "- %d confident pixels fused into %d voxels (voxel %.4g), %d points kept "
        "(seen by >= %d frames, outlier-filtered)" % (r["n_in"], r["n_vox"], r["voxel"], r["n_pts"], r["min_views"]),
        "- confidence gate: percentile %d of each window's above-floor confidence" % r["conf_percentile"],
    ]
    mesh_faces = None
    if r["mesh_info"]:
        mi = r["mesh_info"]
        lines += ["", "## Mesh (mesh_poisson.ply)", ""]
        if "error" in mi:
            lines += ["- FAILED: %s" % mi["error"]]
        else:
            mesh_faces = mi["faces"]
            lines += ["- %d vertices, %d faces (Poisson produced %d faces; %d invented/stray faces trimmed)"
                      % (mi["vertices"], mi["faces"], mi["poisson_faces"], mi["poisson_faces"] - mi["faces"])]
    cam = r.get("camera")
    if cam:
        lines += ["", "## Camera (lens and field of view)", ""]
        u = cam.get("undistort")
        if u:
            lines += ["- lens: %s calibration (%s), %.0f x %.0f deg - frames undistorted to a pinhole "
                      "%.0f x %.0f deg (fx %.1f px at %dx%d) before VGGT"
                      % (u["model"], u.get("source") or "?", u["lens_hfov"], u["lens_vfov"],
                         u["pinhole_hfov"], u["pinhole_vfov"], u["fx"], u["width"], u["height"]),
                      "- VGGT's own focal estimate: fx %.1f px (%.0f deg across) = %.2fx the calibrated %.1f px"
                      % (cam["vggt_fx_px"], cam["vggt_hfov_deg"], cam["ratio"], cam["calibrated_fx_px"])]
        else:
            lines += ["- no lens calibration: frames went to VGGT as recorded",
                      "- VGGT's own focal estimate: fx %.1f px at %d px wide = %.0f deg across. If straight "
                      "edges visibly curve in the frames (a wide-angle/fisheye lens), no single focal length "
                      "fits and the model's angles open up - calibrate the lens (Studio: Drone tab -> Camera "
                      "calibration, or tools/calibrate_camera.py --model auto)"
                      % (cam["vggt_fx_px"], cam["width"], cam["vggt_hfov_deg"])]
    lines += ["", "## Window seams (vision-only Sim(3) alignment)", ""]
    if seams:
        lines += ["- scale correction per seam: min %.4f, median %.4f, max %.4f (1.0 = VGGT kept the same scale)"
                  % (min(scales), float(np.median(scales)), max(scales))]
        if resid:
            lines += ["- median relative residual per seam: median %.4f, worst %.4f" % (float(np.median(resid)), max(resid))]
        if fallbacks:
            lines += ["- WARNING: %d seam(s) had too little confident overlap and fell back to single-camera "
                      "chaining - look for a fast pan or a textureless view there" % fallbacks]
    else:
        lines += ["- single window - no seams"]
    lines += ["", "## Motion blur (Laplacian variance of each frame - lower = blurrier)", ""]
    if blur_all:
        meds = [b["median"] for b in blur_all]
        worst = min(blur_all, key=lambda b: b["worst"])
        lines += ["- per-window median: min %.0f, median %.0f, max %.0f across %d windows"
                  % (min(meds), float(np.median(meds)), max(meds), len(blur_all)),
                  "- blurriest single frame seen: %.0f at frame %d" % (worst["worst"], worst["worst_frame"])]
    else:
        lines += ["- not measured"]
    g = r["georef"]
    lines += ["", "## Georeferencing", ""]
    lines += (["- global Sim(3) to GPS over %d anchors: residual median %.2f m, 90th pct %.2f m"
               % (g["n"], g["resid_median"], g["resid_p90"])] if g else
              ["- none (vision only). Scale is VGGT's own estimate - consistent within the model, "
               "not calibrated to metres."])
    with open(os.path.join(out_dir, "CHECKPOINT_REPORT.md"), "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    log("\n".join(lines))

    # Structured twin of the .md above, for recon_viz.py's live report card and
    # any other tool that would rather not parse markdown.
    summary = {
        "n": r["n"], "frame_stride": r["frame_stride"], "windows": r["windows"], "window": r["window"],
        "gpu": gpu, "vggt_fps": r["n"] / max(r["vggt_s"], 1e-6), "peak_mb": r["peak_mb"],
        "wall_s": r["wall_s"], "span_s": r["span_s"], "n_pts": r["n_pts"], "mesh_faces": mesh_faces,
        "seam_scale_min": min(scales) if scales else None,
        "seam_scale_median": float(np.median(scales)) if scales else None,
        "seam_scale_max": max(scales) if scales else None,
        "seam_fallbacks": fallbacks,
        "blur_median": float(np.median([b["median"] for b in blur_all])) if blur_all else None,
        "blur_worst": min((b["worst"] for b in blur_all), default=None),
        "blur_worst_frame": (min(blur_all, key=lambda b: b["worst"])["worst_frame"] if blur_all else None),
        "georef": {"n": g["n"], "resid_median": g["resid_median"], "resid_p90": g["resid_p90"]} if g else None,
        "camera": r.get("camera"),
    }
    with open(os.path.join(out_dir, "CHECKPOINT_REPORT.json"), "w", newline="\n") as f:
        json.dump(summary, f, indent=2)
    return summary


def _write_trajectory_plot(out_dir, cam_C, georeferenced):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 7))
        if georeferenced:
            ax.plot(cam_C[:, 0], cam_C[:, 1], "b.-", markersize=2)
            ax.set_xlabel("East (m)")
            ax.set_ylabel("North (m)")
        else:
            ax.plot(cam_C[:, 0], -cam_C[:, 2], "b.-", markersize=2)
            ax.set_xlabel("x (right of first view)")
            ax.set_ylabel("forward (-z)")
        ax.plot(cam_C[0, 0], cam_C[0, 1] if georeferenced else -cam_C[0, 2], "go", label="start")
        ax.set_title("Camera trajectory (top view)")
        ax.axis("equal")
        ax.legend()
        fig.savefig(os.path.join(out_dir, "trajectory_check.png"), dpi=120)
        plt.close(fig)
    except Exception as e:                                          # noqa: BLE001
        log("WARNING: trajectory plot failed (%s)" % e)


# ------------------------------------------------------------------ CLI --

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="video file")
    src.add_argument("--from-recording", metavar="DIR", help="a recorded session directory "
                     "(rtvio.studio or live_pipeline --record-only)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--progress", default=None, help="write progress JSON here (used by rtvio.studio)")
    ap.add_argument("--gps", default=None, help="--video only: CSV timestamp_s,lat_deg,lon_deg,alt_m")
    ap.add_argument("--gps-mode", choices=["off", "global"], default=None,
                    help="off: vision only (default for recordings - indoor GPS is noise). "
                         "global: one similarity fit of the whole trajectory to the GPS track "
                         "(default when --gps is given)")
    ap.add_argument("--ref-lat", type=float, default=None)
    ap.add_argument("--ref-lon", type=float, default=None)
    ap.add_argument("--ref-alt", type=float, default=None)
    ap.add_argument("--window-frames", default=WINDOW_FRAMES,
                    help="frames per VGGT pass, or 'auto' (default) to fill the free VRAM")
    ap.add_argument("--overlap", type=int, default=WINDOW_OVERLAP, help="frames shared by neighbouring windows")
    ap.add_argument("--frame-stride", type=int, default=1, help="use every Nth frame (1 = all)")
    ap.add_argument("--max-frames", type=int, default=0, help="stop after this many frames (0 = all)")
    ap.add_argument("--sample-fps", type=float, default=SAMPLE_FPS, help="--video only: 0 = every frame")
    ap.add_argument("--conf-percentile", type=float, default=DEPTH_CONF_PERCENTILE)
    ap.add_argument("--edge-threshold", type=float, default=EDGE_REL_THRESH)
    ap.add_argument("--voxel-factor", type=float, default=1.0,
                    help="fusion voxel = this x one pixel's footprint at the median depth")
    ap.add_argument("--min-views", type=int, default=2, help="drop points seen by fewer frames")
    ap.add_argument("--poisson-depth", type=int, default=10)
    ap.add_argument("--no-mesh", action="store_true", help="skip mesh_poisson.ply")
    ap.add_argument("--no-masking", action="store_true", help="(default) no dynamic-object masking")
    ap.add_argument("--masking", action="store_true", help="mask people/vehicles with YOLO before VGGT")
    ap.add_argument("--masking-preset", choices=["coco", "nadir_aerial"], default="coco")
    ap.add_argument("--intrinsics", default=None, metavar="JSON",
                    help="lens calibration (camera_model profile, e.g. data/drone_camera.json) to undistort "
                         "frames with: for --video, or a recording without its own camera_intrinsics.json")
    ap.add_argument("--no-undistort", action="store_true",
                    help="feed frames to VGGT as recorded even if a lens calibration is available")
    ap.add_argument("--undistort-balance", type=float, default=0.0,
                    help="0 (default) = crop the lens's outermost edge so no black border remains; "
                         "1 = keep the whole field of view (black corners, stretched edges)")
    ap.add_argument("--extras", action="store_true", help="also write LAS, OBJ/GLB and a COLMAP export")
    ap.add_argument("--cell-size-m", type=float, default=1.0, help="--extras 2.5D mesh/DSM cell (GPS mode)")
    ap.add_argument("--live-viz", action="store_true",
                    help="serve a browser view of the incoming frames, the point cloud growing window "
                         "by window, and live stats (fps, confidence-gate keep %%, seam scale/residual, "
                         "motion blur) at http://localhost:PORT, PORT from --viz-port. Preview only - "
                         "see recon_viz.py")
    ap.add_argument("--viz-port", type=int, default=8766)
    ap.add_argument("--open", action="store_true", help="open the --live-viz page in a browser tab")
    ap.add_argument("--no-viz-hold", action="store_true",
                    help="exit as soon as the run finishes instead of keeping the --live-viz server up "
                         "for Ctrl-C - for callers (rtvio.studio) that manage this process's lifecycle "
                         "themselves and need it to actually exit when done")
    args = ap.parse_args()

    opts = dict(window_frames=args.window_frames, overlap=args.overlap, frame_stride=args.frame_stride,
                max_frames=args.max_frames, conf_percentile=args.conf_percentile,
                edge_threshold=args.edge_threshold, voxel_factor=args.voxel_factor,
                min_views=args.min_views, poisson_depth=args.poisson_depth, make_mesh=not args.no_mesh,
                gps_mode=args.gps_mode, ref_lat=args.ref_lat, ref_lon=args.ref_lon, ref_alt=args.ref_alt,
                use_masking=args.masking and not args.no_masking, masking_preset=args.masking_preset,
                extras=args.extras, cell_size_m=args.cell_size_m,
                intrinsics=args.intrinsics, undistort=not args.no_undistort,
                undistort_balance=args.undistort_balance)
    progress = Progress(args.progress)

    viz = None
    if args.live_viz:
        from .recon_viz import ReconViz
        title = os.path.splitext(os.path.basename(args.video))[0] if args.video \
            else os.path.basename(os.path.normpath(args.from_recording))
        viz = ReconViz(args.out, port=args.viz_port, title=title).start()
        url = "http://localhost:%d" % args.viz_port
        log("live viewer: %s" % url)
        if args.open:
            import webbrowser
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    opts["viz"] = viz

    try:
        if args.from_recording:
            reconstruct_from_recording(args.from_recording, args.out, progress_path=args.progress, **opts)
        else:
            reconstruct(args.video, args.gps, args.out, sample_fps=args.sample_fps,
                        progress_path=args.progress, **opts)
    except Exception as e:
        # Keep the last reported stage/window so the UI can say where it died.
        progress.state = _read_json_or_empty(args.progress)
        progress.update(stage="error", error="%s: %s" % (type(e).__name__, e), detail="failed")
        raise
    finally:
        if viz is not None and not args.no_viz_hold:
            # Stay up after the run so the browser tab can fetch the finished
            # mesh/cloud and be explored at leisure - stopping the server the
            # instant reconstruct() returns would race the page's own fetch of
            # /mesh.ply against the report message that triggers it.
            log("done - viewer still live at %s (Ctrl-C to stop)" % url)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        if viz is not None:
            viz.stop()


def _read_json_or_empty(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


if __name__ == "__main__":
    main()
