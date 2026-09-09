"""
Re-emits a recorded fixture as a packet stream. DEBUGGING ONLY.

This is the one place in the package that reads frames from disk, and it
exists so a silent geometry bug can be chased against identical bytes on
every run. A model produced through this path is a model produced for
debugging - live_pipeline.py says so on stdout and in the report.

It reconstructs both clock domains rather than handing out session times
directly: the recorded timeline is pushed back onto a synthetic wall clock
and a synthetic boot clock, with the same ~1e12 gap between them a phone
produces. So the SessionClock, the reorder buffer and the conversion path
are all exercised by a replay exactly as they are by a handset, and a
regression in any of them fails here instead of surviving to the field.
"""
import json
import os
import time

from . import protocol
from .source import KIND_FRAME, KIND_GPS, KIND_IMU

FAKE_BOOT_UPTIME_S = 493122.0
# Per-stream capture-to-send latency, reproduced so the reorder buffer has
# something to reorder. Frames leave later than IMU because a JPEG has to be
# encoded and serialised first.
FRAME_LATENCY_S = 0.045
IMU_LATENCY_S = 0.003
GPS_LATENCY_S = 0.012


class ReplayPacketSource:
    def __init__(self, root, speed=0.0):
        self.root = root
        self.speed = speed

    def _load(self):
        def rd(name):
            with open(os.path.join(self.root, name)) as f:
                return json.load(f)
        return rd("imu_data.json"), rd("gps_data.json"), rd("frame_timestamps.json")

    def packets(self, stats):
        imu, gps, frame_times = self._load()
        frames_dir = os.path.join(self.root, "frames")

        wall0 = time.time()
        boot0 = FAKE_BOOT_UPTIME_S
        events = []

        for idx, t in enumerate(frame_times):
            if t is None:
                continue                    # a hole the recorder marked
            path = os.path.join(frames_dir, "%06d.jpg" % idx)
            if os.path.exists(path):
                events.append((t + FRAME_LATENCY_S, KIND_FRAME, ("frame", path, t)))

        batch = []
        for s in imu:
            batch.append(s)
            if len(batch) == 10:
                events.append((batch[-1]["timestamp"] + IMU_LATENCY_S, KIND_IMU,
                               ("imu", list(batch), None)))
                batch = []
        if batch:
            events.append((batch[-1]["timestamp"] + IMU_LATENCY_S, KIND_IMU,
                           ("imu", list(batch), None)))

        for g in gps:
            events.append((g["timestamp"] + GPS_LATENCY_S, KIND_GPS, ("gps", g, None)))

        events.sort(key=lambda e: e[0])
        started = time.monotonic()

        for send_t, kind, payload in events:
            if self.speed > 0:
                delay = started + send_t / self.speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            t_recv = time.monotonic()
            tag, body, t_dev = payload

            if tag == "frame":
                with open(body, "rb") as f:
                    jpeg = f.read()
                pkt = protocol.FramePacket(int(round((wall0 + t_dev) * 1e3)), 0, 0, jpeg)
                # width/height are recovered by the decoder; the recorder does
                # not store them separately and the JPEG is authoritative.
                import cv2, numpy as np
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                h, w = img.shape[:2]
                pkt = protocol.FramePacket(pkt.timestamp_ms, w, h, jpeg)
                stats.bytes += 21 + len(jpeg)
                yield KIND_FRAME, pkt, t_recv
            elif tag == "imu":
                samples = [protocol.ImuSample(
                    int(round((boot0 + s["timestamp"]) * 1e9)),
                    tuple(s["accel_body_xyz"]), tuple(s["gyro_body_xyz"])) for s in body]
                stats.bytes += 3 + len(samples) * protocol.IMU_SAMPLE_BYTES
                yield KIND_IMU, samples, t_recv
            else:
                pkt = protocol.GpsPacket(
                    int(round((wall0 + body["timestamp"]) * 1e3)),
                    body["latitude_deg"], body["longitude_deg"],
                    body["altitude_m"], body.get("accuracy_m", -1.0))
                stats.bytes += 1 + protocol.GPS_BYTES
                yield KIND_GPS, pkt, t_recv
