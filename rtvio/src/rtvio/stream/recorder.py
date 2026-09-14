"""
Fixture writer. A TEST HARNESS, not a data path.

Subscribes to the same packet stream the reconstruction does and writes it
to disk in a form `replay.py` can re-emit byte-for-byte. The point is
reproducibility: three of the known integration hazards (clock domains,
frame timing, the camera-IMU extrinsic) fail SILENTLY - no exception,
plausible-looking output, wrong geometry. Fixing a sign error in an
extrinsic means changing one thing and seeing the output move, and against
a live stream every run is different footage, lighting, GPS quality and
drop pattern. One recorded session is the same bytes every time.

Two properties keep this honest:

  * It runs on its own thread behind a BOUNDED queue. If the disk cannot
    keep up it drops and counts, never blocks - so enabling recording can
    slow the disk but cannot stall the reconstruction lane or change its
    timing enough to alter the model.
  * It writes JPEGs exactly as received. The phone already sends lossy
    frames; re-encoding them to H.264 would stack a second generation of
    loss on top, costing feature matches and therefore depth quality. An
    mp4, if some other tool needs one, belongs alongside this - never in
    place of it.

The layout mirrors what the now-removed batch `ingest.Dataset` loader once
read (imu_data.json / gps_data.json / flight_config.json) plus
`frame_timestamps.json`, which that loader never had - real captures drop
frames and a constant-fps assumption doesn't hold.
"""
import json
import os
import queue
import threading

QUEUE_LIMIT = 256          # ~50 MB of pending JPEGs before dropping


class SessionRecorder:
    def __init__(self, out_dir, intrinsics=None):
        self.out_dir = out_dir
        self.frames_dir = os.path.join(out_dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.intrinsics = intrinsics

        self.imu = []
        self.gps = []
        self.frame_times = []
        self.n_frames = 0
        self.dropped = 0

        self._q = queue.Queue(maxsize=QUEUE_LIMIT)
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ writer --

    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            path, blob = item
            try:
                with open(path, "wb") as f:
                    f.write(blob)
            except OSError:
                self.dropped += 1

    # ------------------------------------------------------- subscription --

    def on_session_start(self, clock, _hint):
        self.clock = clock

    def on_frame(self, t_s, pkt):
        idx = self.n_frames
        self.n_frames += 1
        self.frame_times.append(round(t_s, 6))
        path = os.path.join(self.frames_dir, "%06d.jpg" % idx)
        try:
            self._q.put_nowait((path, pkt.jpeg))
        except queue.Full:
            self.dropped += 1
            self.frame_times[-1] = None      # keep index alignment, mark the hole

    def on_imu(self, t_s, sample):
        # Session-relative float seconds, which is what the pipeline reads -
        # NOT the wire value. Bias fields are omitted: they exist only in the
        # synthetic generator's output and nothing reads them.
        self.imu.append({
            "timestamp": round(t_s, 6),
            "accel_body_xyz": list(sample.accel),
            "gyro_body_xyz": list(sample.gyro),
        })

    def on_gps(self, t_s, pkt, sigma_m):
        self.gps.append({
            "timestamp": round(t_s, 6),
            "latitude_deg": pkt.lat_deg,
            "longitude_deg": pkt.lon_deg,
            "altitude_m": pkt.altitude_m,
            "accuracy_m": pkt.accuracy_m,
            "sigma_m": sigma_m,
        })

    def on_session_end(self, stats):
        self._q.put(None)
        self._thread.join(timeout=30)

        def dump(name, obj):
            with open(os.path.join(self.out_dir, name), "w") as f:
                json.dump(obj, f)

        dump("imu_data.json", self.imu)
        dump("gps_data.json", self.gps)
        # Real per-frame timestamps. ingest.frame_timestamp() derives time from
        # frame INDEX, which is only true at a perfectly constant frame rate;
        # the app drops frames on purpose when the link congests, and one drop
        # shifts every later frame's time by 1/fps with no exception raised.
        dump("frame_timestamps.json", self.frame_times)
        if self.intrinsics:
            dump("camera_intrinsics.json", self.intrinsics)

        span = 0.0
        real = [t for t in self.frame_times if t is not None]
        if len(real) > 1:
            span = real[-1] - real[0]
        dump("flight_config.json", {
            "fps": round(stats.measured_fps, 3),
            "n_frames": self.n_frames,
            "total_time_s": round(span, 3),
            "gps_noise_std_m": None,
            "note": "fps is the MEASURED median frame interval, not a setting. "
                    "Prefer frame_timestamps.json over index/fps for frame timing.",
        })
        dump("session_meta.json", {
            "frames_received": stats.frames,
            "frames_written": self.n_frames - self.dropped,
            "write_dropped": self.dropped,
            "frame_gaps": stats.frame_gaps,
            "late_dropped": stats.late_dropped,
            "imu_samples": stats.imu_samples,
            "gps_fixes": stats.gps_fixes,
            # self.clock only exists once on_session_start has fired -
            # StreamSession.run() skips it entirely when the stream never
            # establishes a usable clock offset (e.g. zero IMU samples: see
            # its "stream carried no usable pairing" SystemExit path), and
            # still calls on_session_end right before raising that. A short
            # or IMU-less test recording is exactly the first thing someone
            # verifying --record-only would try - losing the fixture (frames/
            # GPS this recorder DID capture) to an AttributeError here on top
            # of the SystemExit that's coming anyway would hide real captured
            # data behind a confusing crash.
            "boot_to_wall_s": getattr(getattr(self, "clock", None), "boot_to_wall_s", None),
            "wall_t0_s": getattr(getattr(self, "clock", None), "wall_t0_s", None),
        })
        msg = "fixture written to %s (%d frames, %d imu, %d gps)" % (
            self.out_dir, self.n_frames, len(self.imu), len(self.gps))
        if self.dropped:
            msg += " - %d frames DROPPED by the writer; the fixture is incomplete" % self.dropped
        print(msg)
