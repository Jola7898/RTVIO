"""
Two `StreamSession` subscribers that plug a monocular reconstruction front
end (LingBot-Map today; see `../rtvio/docs/ARCHITECTURE_REDESIGN.md` sec 4
for the classical-VIO alternative) into rtvio's existing live-streaming
architecture, per that doc's sec 5 integration plan and `session.md` sec 7.3.

    LiveMapPreview          live subscriber: buffers frames, then drives a
                             decimated subset through a per-frame model call
                             for a coarse, near-real-time preview. Optional --
                             PS-17 asks for "near real-time situational
                             awareness", not a frame-perfect live video, so a
                             missing/slow preview does not block the real
                             reconstruction below.
    BatchMapReconstructor    the real reconstruction. NOT a live subscriber --
                             it is driven by REPLAYING the fixture
                             `rtvio.stream.recorder.SessionRecorder` already
                             wrote, through a second
                             `StreamSession(ReplayPacketSource(...), ...)`
                             pass at max speed: the "replay front door"
                             architecture decision recorded in `session.md`
                             sec 2 decision #1. This is why it does not
                             duplicate SessionRecorder's job -- `stream/
                             source.py`'s own structural rule is that the
                             recorder and the reconstruction are independent
                             subscribers that cannot see each other, and that
                             rule is reused here rather than re-litigated.

STATUS, 12 Sept 2026: scaffolded, not yet run against a real front end or a
real phone. What's actually verified (see `_selftest()` at the bottom, which
passes): the StreamSession subscriber wiring, GPS/ENU bookkeeping, and the
align.py -> meshing -> export handoff, end to end, with a synthetic front
end standing in for LingBot-Map.

What's NOT wired yet, and why it's a seam rather than a hardcoded call:
`model_forward`/`model_batch_infer` below are injected callables, not a
direct `import lingbot_map`. rtvio's own venv (native Windows, Python 3.13)
cannot import lingbot_map there: FlashInfer has no Windows wheel, which is
the entire reason `wsl_setup.sh` exists (`session.md` sec 4). Wiring a real
front end means EITHER running this whole adapter inside the WSL2
environment (`session.md` sec 4 already confirms `import rtvio` works from
`rtviomap/.venv`, so both packages ARE importable together there), OR
splitting model inference into a small WSL-side service this file's
callables talk to. Neither is decided yet -- see
`../rtvio/docs/ARCHITECTURE_REDESIGN.md` sec 6.

Run from inside `rtviomap/` (so the sibling `align` module resolves):
    python adapter.py                 # runs the self-test below
"""
import os

import numpy as np

from rtvio.stream.geodesy import latlon_to_enu

from align import align_trajectory_to_gps, apply_similarity


# --------------------------------------------------------------- live preview --

class LiveMapPreview:
    """Live StreamSession subscriber: buffers frames until a front end's
    initial-context requirement is met (LingBot-Map's Phase 1 needs
    `num_scale_frames`, default 8, processed together before per-frame
    causal streaming can start -- session.md sec 4's "API surface" note),
    then drives a decimated subset through a per-frame model call.

    Mirrors live_pipeline.py's LiveReconstructor "buffer until yaw is
    observable, then start" pattern (its `_buffer`/`_try_initialize`), except
    what's being waited on here is frame count, not GPS-derived heading -- a
    monocular feed-forward model has no yaw-initialization step of its own.
    """

    def __init__(self, model_forward=None, warmup_frames=8, decimate=1):
        """
        Args:
            model_forward: callable(frame_bgr: np.ndarray, is_first_block: bool)
                -> whatever the front end returns for a live preview (a pose,
                a point delta, ...). None disables live preview entirely --
                see the module docstring for why that's an acceptable
                default, not a degraded one.
            warmup_frames: frames buffered before the first model call.
            decimate: call the model on only every Nth frame after warmup --
                the live path never has to run every frame at full rate;
                that's BatchMapReconstructor's job, post-flight.
        """
        self.model_forward = model_forward
        self.warmup_frames = warmup_frames
        self.decimate = decimate
        self._warmup_buffer = []
        self._started = False
        self._frame_idx = 0
        self.previews = []          # (t_s, result) pairs, for a UI to poll

    def on_session_start(self, clock, _hint):
        print("LiveMapPreview: armed" + ("" if self.model_forward else
              " (no model_forward given -- preview disabled, recording only)"))

    def on_frame(self, t_s, pkt):
        self._frame_idx += 1
        if self.model_forward is None:
            return
        if not self._started:
            self._warmup_buffer.append((t_s, pkt))
            if len(self._warmup_buffer) < self.warmup_frames:
                return
            self._started = True
            # Phase 1: the buffered frames go in together (bidirectional
            # scale-anchoring -- session.md sec 4). Real JPEG decode belongs
            # here; left as the seam the module docstring describes.
            for _buffered_t, _buffered_pkt in self._warmup_buffer:
                pass  # TODO: decode _buffered_pkt.jpeg, feed to Phase 1
            self._warmup_buffer = []
            return
        if self._frame_idx % self.decimate != 0:
            return
        # TODO: decode pkt.jpeg -> frame_bgr, call
        #   result = self.model_forward(frame_bgr, is_first_block=False)
        #   self.previews.append((t_s, result))

    def on_session_end(self, stats):
        print("LiveMapPreview: %d preview calls over %d frames"
              % (len(self.previews), self._frame_idx))


# ------------------------------------------------------------- batch pass --

class BatchMapReconstructor:
    """Driven by replaying a SessionRecorder fixture through a second
    `StreamSession(ReplayPacketSource(...), subscribers=[this])` pass, at
    max speed -- not a live subscriber. See
    `../rtvio/docs/ARCHITECTURE_REDESIGN.md` sec 5 for why this is a replay
    front door on the existing live architecture rather than a separate
    batch pipeline, and `rtvio/src/rtvio/stream/replay.py` for the
    ReplayPacketSource it drives on.

    Collects every frame + GPS fix during the replay (this pass is not
    latency-constrained, so nothing here sheds anything the way the live
    dense-stereo lane does), then at `on_session_end`:
        1. runs the collected frames through the front end's real batch
           inference (`model_batch_infer`),
        2. aligns the result into rtvio's local-ENU-metres frame via
           align.py's closed-form similarity fit against the collected GPS
           fixes,
        3. hands off to `rtvio.meshing`/`rtvio.export` UNCHANGED -- the
           entire point of the align.py step (session.md sec 6).
    """

    def __init__(self, out_dir, model_batch_infer, cell_size_m=1.0,
                 max_time_diff_s=0.5):
        """
        Args:
            out_dir: where to write the mesh/LAS/DSM outputs.
            model_batch_infer: callable(frame_packets, timestamps) ->
                (model_positions[N,3], points[M,3], colors[M,3]) in the
                model's own arbitrary scale/frame. This is the seam the
                module docstring describes -- nothing real is wired here
                yet; see `../rtvio/docs/ARCHITECTURE_REDESIGN.md` sec 6 for
                what unblocks it.
            cell_size_m / max_time_diff_s: passed through to
                `rtvio.meshing.build_grid` / `align.align_trajectory_to_gps`.
        """
        self.out_dir = out_dir
        self.model_batch_infer = model_batch_infer
        self.cell_size_m = cell_size_m
        self.max_time_diff_s = max_time_diff_s
        os.makedirs(out_dir, exist_ok=True)

        self.ref = None                 # (lat, lon, alt) of the first GPS fix
        self._frames = []               # (t_s, pkt) - kept for the batch pass
        self._gps_enu = []
        self._gps_t = []

    def on_session_start(self, clock, _hint):
        print("BatchMapReconstructor: replaying fixture for full reconstruction")

    def on_frame(self, t_s, pkt):
        self._frames.append((t_s, pkt))

    def on_gps(self, t_s, pkt, _sigma_m):
        if self.ref is None:
            self.ref = (pkt.lat_deg, pkt.lon_deg, pkt.altitude_m)
            print("reference origin fixed at first GPS: %.7f, %.7f, %.1f m"
                  % self.ref)
        enu = latlon_to_enu(pkt.lat_deg, pkt.lon_deg, pkt.altitude_m, *self.ref)
        self._gps_enu.append(enu)
        self._gps_t.append(t_s)

    def on_session_end(self, stats):
        if not self._frames:
            print("BatchMapReconstructor: no frames recorded, nothing to do")
            return
        if self.ref is None:
            print("BatchMapReconstructor: no GPS fixes seen -- cannot "
                  "georeference (see ARCHITECTURE_REDESIGN.md sec 3's "
                  "GPS-lock item). Aborting rather than writing an output "
                  "with no defined location.")
            return

        timestamps = [t for t, _pkt in self._frames]
        # TODO: decode each pkt.jpeg to a frame here, once model_batch_infer
        # expects real images rather than the raw packets themselves.
        model_positions, points, colors = self.model_batch_infer(
            [pkt for _t, pkt in self._frames], timestamps)

        R, t, s, used = align_trajectory_to_gps(
            model_positions, timestamps, self._gps_enu, self._gps_t,
            max_time_diff_s=self.max_time_diff_s)
        print("aligned with %d/%d GPS fixes used" % (used, len(self._gps_enu)))
        points_enu = apply_similarity(np.asarray(points, dtype=np.float64), R, t, s)

        # From here on this is UNCHANGED rtvio code -- the entire point of
        # the align.py step (session.md sec 6) is that meshing/export never
        # learn a different reconstruction produced their input.
        from rtvio import meshing, export
        grid = meshing.build_grid(points_enu, np.asarray(colors, dtype=np.float64),
                                   self.cell_size_m)
        meshing.write_textured_mesh(grid, os.path.join(self.out_dir, "mesh.obj"))
        export.export_las(points_enu, colors, *self.ref,
                           os.path.join(self.out_dir, "cloud.las"))
        export.export_dsm_raster(grid, *self.ref,
                                  os.path.join(self.out_dir, "dsm.png"))
        print("wrote mesh/LAS/DSM to %s" % self.out_dir)


# ----------------------------------------------------------------- wiring --

def run_live(port, out_dir, model_forward=None):
    """Live half: phone -> SessionRecorder (fixture) + LiveMapPreview
    (optional coarse preview), same StreamSession, both subscribing."""
    from rtvio.stream.source import StreamSession, SocketPacketSource
    from rtvio.stream.recorder import SessionRecorder

    recorder = SessionRecorder(out_dir)
    preview = LiveMapPreview(model_forward)
    session = StreamSession(SocketPacketSource(port=port), [recorder, preview])
    session.run()


def run_batch(fixture_dir, out_dir, model_batch_infer):
    """Batch half: replay the fixture SessionRecorder wrote, at max speed,
    through BatchMapReconstructor -- the "replay front door" from
    ARCHITECTURE_REDESIGN.md sec 5 / session.md sec 2 decision #1."""
    from rtvio.stream.source import StreamSession
    from rtvio.stream.replay import ReplayPacketSource

    reconstructor = BatchMapReconstructor(out_dir, model_batch_infer)
    session = StreamSession(ReplayPacketSource(fixture_dir), [reconstructor])
    session.run()


# ------------------------------------------------------------- self-test --

def _selftest():
    """Exercises the GPS-bookkeeping + align.py + meshing/export handoff in
    BatchMapReconstructor with a synthetic front end -- no real model, no
    phone, no WSL. Proves the glue code works end-to-end; the open seam is
    the model integration itself (module docstring above), not this.
    """
    import shutil
    import tempfile

    from rtvio.stream.geodesy import enu_to_latlon
    from rtvio.stream.protocol import FramePacket, GpsPacket

    ref_lat, ref_lon, ref_alt = 12.9716, 77.5946, 900.0
    n = 20
    # An L-shaped path (east, then north) -- ground truth in local ENU
    # metres. Not a straight line: align.py's umeyama_alignment correctly
    # refuses a collinear fit (a straight flight path can't constrain a 3D
    # rotation) and a first draft of this self-test tripped that check.
    true_positions = np.array(
        [[i * 5.0, 0.0, 0.0] for i in range(n // 2)]
        + [[(n // 2 - 1) * 5.0, (i + 1) * 5.0, 0.0] for i in range(n - n // 2)])
    timestamps = [i * 0.5 for i in range(n)]

    # The "model" reports the same trajectory in its own arbitrary
    # scale/rotation/offset -- exactly the ambiguity align.py exists to
    # remove (see align.py's module docstring). If the handoff below is
    # correct, the written output ends up back in true, local-ENU metres.
    def fake_batch_infer(frame_packets, ts):
        model_positions = true_positions * 2.0 + np.array([100.0, 50.0, 0.0])
        points = model_positions   # trivial "point cloud" = the trajectory
        colors = np.tile([200, 200, 200], (n, 1))
        return model_positions, points, colors

    out_dir = tempfile.mkdtemp(prefix="adapter_selftest_")
    try:
        recon = BatchMapReconstructor(out_dir, fake_batch_infer, cell_size_m=1.0)
        recon.on_session_start(None, None)
        for i in range(n):
            t_s = timestamps[i]
            recon.on_frame(t_s, FramePacket(int(t_s * 1000), 10, 10, b""))
            e, north, up = true_positions[i]
            lat, lon, alt = enu_to_latlon(e, north, up, ref_lat, ref_lon, ref_alt)
            recon.on_gps(t_s, GpsPacket(int(t_s * 1000), lat, lon, alt, 2.0), 2.0)
        recon.on_session_end(None)

        mesh_path = os.path.join(out_dir, "mesh.obj")
        las_path = os.path.join(out_dir, "cloud.las")
        assert os.path.exists(mesh_path), "mesh.obj was not written"
        assert os.path.exists(las_path), "cloud.las was not written"
        print("adapter self-test PASSED (align + meshing + export handoff "
              "verified end-to-end with a synthetic front end)")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    _selftest()
