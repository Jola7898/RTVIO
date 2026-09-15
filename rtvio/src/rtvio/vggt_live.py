"""
Live VGGT reconstruction: process windows of frames as they arrive over the
network, instead of waiting for the whole flight to land on disk first.

    python -m rtvio.vggt_live --port 5555 --out data/outputs/live1

Point the phone's Settings -> Server IP at this machine and START STREAMING
(not RECORD LOCALLY - that path has no network connection for this to
receive over; reconstruct it afterward with --from-recording instead).

WHY THIS EXISTS, AND WHAT IT DOES NOT BUY YOU
VGGT-1B runs SLOWER than real time at realistic capture rates - a 24-30 fps
phone stream against the ~5-8 frames/s of VGGT throughput measured on a
16 GB RTX 5070 Ti (see any CHECKPOINT_REPORT.md's own "Wall time ...
(0.17-0.19x real time)" line). So this does NOT make reconstruction finish
moments after the flight does - the backlog of unprocessed frames grows for
as long as capture continues, at exactly that same ~0.2x rate. What it DOES
buy: processing that would otherwise wait for the entire recording to land
on disk first now starts the moment the first window's frames arrive, so
total wall-clock time from CONNECT to a finished model drops from
`capture_time + vggt_time` to roughly `max(capture_time, vggt_time)` plus one
window's tail - a real saving whenever vggt_time is the larger of the two
(usually true), bounded by the SHORTER of the two stages, never by making
VGGT itself faster.

Shares its window-to-window algorithm (vggt_reconstruct._process_window /
_finalize_and_write) with the batch --from-recording path exactly, so a live
run and a from-recording run of the same footage produce the same geometry -
verified by replaying an already-reconstructed session's frames back through
this receiver and diffing CHECKPOINT_REPORT.md's seam/georef numbers against
the original batch run.

WHAT IS DIFFERENT FROM THE BATCH PATH
 - The very last (possibly undersized) window is whatever is left when the
   stream ends, not the batch path's _plan_next tail-absorption heuristic -
   a minor loss of that optimisation, not a correctness issue.
 - Window processing runs synchronously on the packet-reading thread: while
   a window's ~10-15s VGGT pass runs, incoming frames queue up exactly as
   they would against any other slow receiver (the app's video queue evicts
   its oldest frame once full - "video drops, control waits" in
   rtvioapk/README.md's design notes). IMU/GPS still get through; video does
   not. This is a deliberate simplicity choice: a background worker thread
   would remove the drop but risks two CUDA calls overlapping if a window is
   still running when the next one becomes ready, which is worse than a
   dropped preview frame.
 - Every received frame is still written to `<out>/frames/%06d.jpg` plus the
   same frame_timestamps.json/gps_data.json sidecars a phone recording uses,
   so the output directory is itself a valid --from-recording session if you
   ever want to reprocess it with different flags.
"""
import argparse
import json
import os
import time

from .vggt_reconstruct import (
    AUTO_MIN_WINDOW,
    DEPTH_CONF_PERCENTILE,
    EDGE_REL_THRESH,
    WINDOW_OVERLAP,
    FrameLoader,
    Progress,
    _finalize_and_write,
    _load_vggt,
    _process_window,
    _ReconState,
    auto_window_frames,
    log,
)


class VGGTLiveReconstructor:
    """StreamSession subscriber (see rtvio.stream.source): writes frames to
    disk as they arrive and runs a VGGT window the moment enough new ones
    exist for it, via vggt_reconstruct's shared per-window/finalize code."""

    def __init__(self, out_dir, window_frames="auto", overlap=WINDOW_OVERLAP,
                conf_percentile=DEPTH_CONF_PERCENTILE, edge_threshold=EDGE_REL_THRESH,
                voxel_factor=1.0, min_views=2, poisson_depth=10, make_mesh=True,
                gps_mode=None, ref_lat=None, ref_lon=None, ref_alt=None, extras=False,
                cell_size_m=1.0, progress_path=None, viz=None):
        self.out_dir = out_dir
        self.frames_dir = os.path.join(out_dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.window_frames = window_frames
        self.overlap = overlap
        self.conf_percentile = conf_percentile
        self.edge_threshold = edge_threshold
        self.voxel_factor = voxel_factor
        self.min_views = min_views
        self.poisson_depth = poisson_depth
        self.make_mesh = make_mesh
        self.gps_mode = gps_mode
        self.ref_lat, self.ref_lon, self.ref_alt = ref_lat, ref_lon, ref_alt
        self.extras = extras
        self.cell_size_m = cell_size_m
        self.progress = Progress(progress_path)
        self.viz = viz

        self.frame_paths = []          # shared by reference with self.loader.paths once it exists
        self.frame_times = []
        self.gps_track = []
        self.state = _ReconState()
        self.loader = None
        self.window = None
        self.start = 0                 # index the NEXT window will begin at
        self.window_times = []
        self.wall_t0 = None             # set in on_session_start - not at listen time
        self.vggt_s = 0.0
        self.model = None
        self.device = None
        self.dtype = None

    # ---------------------------------------------------- StreamSession API

    def on_session_start(self, clock, _hint):
        import torch
        self.wall_t0 = time.monotonic()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            torch.backends.cudnn.benchmark = False
        else:
            log("WARNING: no CUDA device - running VGGT on the CPU will be extremely slow")
        self.progress.update(stage="loading", detail="loading VGGT-1B", fraction=0.0)
        self.model, self.dtype = _load_vggt(self.device)

    def on_frame(self, t_s, pkt):
        idx = len(self.frame_paths)
        path = os.path.join(self.frames_dir, "%06d.jpg" % idx)
        with open(path, "wb") as f:
            f.write(pkt.jpeg)
        self.frame_paths.append(path)
        self.frame_times.append(round(t_s, 6))
        if self.viz is not None:
            self.viz.push_frame(pkt.jpeg)   # the actual incoming network frame, not a resized proxy

        if self.loader is None:
            # FrameLoader keeps the exact list object we keep appending to,
            # so later frames need no separate hand-off into it.
            self.loader = FrameLoader(self.frame_paths)
            tokens = (self.loader.H // 14) * (self.loader.W // 14)
            log("input: live stream, %dx%d%s -> VGGT %dx%d (%d tokens/frame)"
                % (self.loader.src_size[0], self.loader.src_size[1],
                   ", portrait -> rotated to landscape" if self.loader.rotated else "",
                   self.loader.W, self.loader.H, tokens))
            if self.window_frames in (None, "auto", "0", 0):
                import torch
                free_mb = torch.cuda.mem_get_info()[0] / 1e6 if self.device == "cuda" else 8000
                self.window = auto_window_frames(tokens, self.loader.H * self.loader.W, free_mb)
                log("auto window: %d frames per VGGT pass (%.0f MB VRAM free)" % (self.window, free_mb))
            else:
                self.window = int(self.window_frames)
            self.overlap = max(1, min(int(self.overlap), self.window // 2))

        self._drain_ready_windows()

    def _drain_ready_windows(self):
        import torch
        while len(self.frame_paths) - self.start >= self.window:
            end = self.start + self.window
            try:
                kept, thresh, seam, dt, blur_stats = _process_window(
                    self.model, self.device, self.dtype, self.loader, None,
                    self.start, end, self.conf_percentile, self.edge_threshold,
                    self.voxel_factor, self.state, prefetch_range=None, viz=self.viz)
            except torch.OutOfMemoryError:
                torch.cuda.empty_cache()
                if self.window <= AUTO_MIN_WINDOW:
                    raise
                self.window = max(AUTO_MIN_WINDOW, int(self.window * 0.75))
                self.overlap = min(self.overlap, self.window // 2)
                log("  out of VRAM - retrying with %d-frame windows" % self.window)
                continue
            self._log_window(self.start, end, dt, kept, thresh, seam, blur_stats, tail=False)
            self.loader.release_before(end - self.overlap)
            self.start = end - self.overlap

    def _log_window(self, start, end, dt, kept, thresh, seam, blur_stats, tail):
        import torch
        self.window_times.append((end - start, dt))
        self.vggt_s += dt
        log("window %d: frames [%d:%d) %.2fs (%.1f frames/s), %.0f%% px kept, conf>=%.3g%s%s"
            % (self.state.wi, start, end, dt, (end - start) / dt, 100 * kept, thresh,
               "" if seam is None else ", seam s=%.4f resid=%s" % (
                   seam["scale"], "%.3f" % seam["median_rel_residual"] if seam["median_rel_residual"] is not None else "-"),
               "  [live, tail]" if tail else "  [live]"))
        if self.viz is not None:
            self.viz.push_window({
                "window": self.state.wi, "windows_est": None,
                "frames_done": end, "frames_total": None,
                "fps": round((end - start) / dt, 2), "kept_frac": kept, "conf_thresh": thresh,
                "gpu": torch.cuda.get_device_name(0) if self.device == "cuda" else None,
                "seam": ({"method": seam["method"], "scale": seam["scale"],
                          "median_rel_residual": seam.get("median_rel_residual")} if seam is not None else None),
                "blur": blur_stats,
            })

    def on_gps(self, t_s, pkt, sigma_m):
        self.gps_track.append({"t": round(t_s, 6), "lat": pkt.lat_deg, "lon": pkt.lon_deg, "alt": pkt.altitude_m})

    def on_session_end(self, stats):
        import torch
        n = len(self.frame_paths)
        if self.loader is None or n < 2:
            log("stream carried fewer than 2 frames - nothing to reconstruct")
            return

        if n - self.start >= 2:
            end = n
            try:
                kept, thresh, seam, dt, blur_stats = _process_window(
                    self.model, self.device, self.dtype, self.loader, None,
                    self.start, end, self.conf_percentile, self.edge_threshold,
                    self.voxel_factor, self.state, prefetch_range=None, viz=self.viz)
                self._log_window(self.start, end, dt, kept, thresh, seam, blur_stats, tail=True)
                self.start = end
            except torch.OutOfMemoryError:
                log("  out of VRAM on the final (tail) window - it is dropped; "
                    "everything up to frame %d is still reconstructed" % self.start)

        if self.state.acc is None:
            log("no window ever completed - nothing to reconstruct")
            return

        gps_mode = self.gps_mode
        if self.gps_track and gps_mode is None:
            gps_mode = "global"     # same auto-detect as reconstruct_from_recording

        peak_mb = torch.cuda.max_memory_allocated() / 1e6 if self.device == "cuda" else 0.0
        gpu_name = torch.cuda.get_device_name(0) if self.device == "cuda" else None
        del self.state.prev
        if self.device == "cuda":
            torch.cuda.empty_cache()
        self.loader.close()

        with open(os.path.join(self.out_dir, "frame_timestamps.json"), "w") as f:
            json.dump(self.frame_times, f)
        with open(os.path.join(self.out_dir, "gps_data.json"), "w") as f:
            json.dump([{"timestamp": g["t"], "latitude_deg": g["lat"], "longitude_deg": g["lon"],
                       "altitude_m": g["alt"], "accuracy_m": -1.0} for g in self.gps_track], f)

        _finalize_and_write(
            self.state, self.frame_paths, self.frame_times, self.loader, self.out_dir, self.progress,
            gps_mode, self.gps_track, self.ref_lat, self.ref_lon, self.ref_alt,
            self.min_views, self.make_mesh, self.poisson_depth, self.extras, self.cell_size_m,
            self.window, self.overlap, self.vggt_s, lambda: time.monotonic() - self.wall_t0,
            self.conf_percentile, 1, peak_mb, gpu_name=gpu_name, viz=self.viz)


def build_argparser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--progress", default=None, help="write progress JSON here (used by rtvio.studio)")
    ap.add_argument("--window-frames", default="auto")
    ap.add_argument("--overlap", type=int, default=WINDOW_OVERLAP, help="frames shared by neighbouring windows")
    ap.add_argument("--conf-percentile", type=float, default=DEPTH_CONF_PERCENTILE)
    ap.add_argument("--edge-threshold", type=float, default=EDGE_REL_THRESH)
    ap.add_argument("--voxel-factor", type=float, default=1.0)
    ap.add_argument("--min-views", type=int, default=2, help="drop points seen by fewer frames")
    ap.add_argument("--poisson-depth", type=int, default=10)
    ap.add_argument("--no-mesh", action="store_true", help="skip mesh_poisson.ply")
    ap.add_argument("--gps-mode", choices=["off", "global"], default=None,
                    help="default: auto - global iff the stream carried a GPS fix")
    ap.add_argument("--ref-lat", type=float, default=None)
    ap.add_argument("--ref-lon", type=float, default=None)
    ap.add_argument("--ref-alt", type=float, default=None)
    ap.add_argument("--extras", action="store_true", help="also write LAS, OBJ/GLB and a COLMAP export")
    ap.add_argument("--cell-size-m", type=float, default=1.0, help="--extras 2.5D mesh/DSM cell (GPS mode)")
    ap.add_argument("--live-viz", action="store_true",
                    help="serve a browser view of the incoming phone frames, the point cloud growing "
                         "window by window, and live stats at http://localhost:PORT, PORT from "
                         "--viz-port. Preview only - see recon_viz.py")
    ap.add_argument("--viz-port", type=int, default=8766)
    ap.add_argument("--open", action="store_true", help="open the --live-viz page in a browser tab")
    return ap


def main():
    args = build_argparser().parse_args()
    os.makedirs(args.out, exist_ok=True)
    from .stream.source import SocketPacketSource, StreamSession
    log("RTVIO live VGGT receiver on %s:%d (Ctrl-C to stop) -> %s" % (args.host, args.port, args.out))

    viz = None
    if args.live_viz:
        from .recon_viz import ReconViz
        viz = ReconViz(args.out, port=args.viz_port, title=os.path.basename(os.path.normpath(args.out))).start()
        url = "http://localhost:%d" % args.viz_port
        log("live viewer: %s" % url)
        if args.open:
            import threading
            import webbrowser
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    # SocketPacketSource accepts exactly one connection per call, then closes
    # its listening socket - fine for a real phone, but the app's own
    # reachability check (Settings screen and the main screen's STREAM/RECORD
    # LOCALLY button) is a bare connect-then-immediately-disconnect probe with
    # no data behind it, sent every few seconds while idle. Without looping
    # here, the very first probe after this process starts consumes the one
    # connection slot and the process exits before START STREAMING is ever
    # tapped for real - worse, StreamSession.run() raises SystemExit for a
    # connection that carried zero frames AND zero IMU samples (the right
    # call for live_pipeline.py, where that means a real flight attempt got
    # nothing; wrong here, where it usually just means a probe), so that has
    # to be caught too, not merely a small frame count checked afterward.
    while True:
        rec = VGGTLiveReconstructor(
            args.out, window_frames=args.window_frames, overlap=args.overlap,
            conf_percentile=args.conf_percentile, edge_threshold=args.edge_threshold,
            voxel_factor=args.voxel_factor, min_views=args.min_views,
            poisson_depth=args.poisson_depth, make_mesh=not args.no_mesh,
            gps_mode=args.gps_mode, ref_lat=args.ref_lat, ref_lon=args.ref_lon, ref_alt=args.ref_alt,
            extras=args.extras, cell_size_m=args.cell_size_m, progress_path=args.progress, viz=viz,
        )
        source = SocketPacketSource(args.host, args.port)
        try:
            StreamSession(source, [rec]).run()
        except SystemExit as e:
            log("(%s - probably a reachability check, not a real stream; listening again)" % e)
            continue
        if len(rec.frame_paths) >= 2:
            break
        log("(that connection carried no data - probably a reachability check, not a real "
            "stream; listening again)")
    if viz is not None:
        # Stay up so the browser tab can fetch the finished mesh/cloud once
        # the 'report' SSE message tells it to - see the matching comment in
        # vggt_reconstruct.main().
        log("done - viewer still live at %s (Ctrl-C to stop)" % url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        viz.stop()


if __name__ == "__main__":
    main()
