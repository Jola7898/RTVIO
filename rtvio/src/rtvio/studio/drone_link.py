"""
The drone side of rtvio.studio: the drone's live video and MAVLink
telemetry, and recordings in the same session layout the phone writes.

Ported from the idronam branch's capture-bridge, a Node tool built by
reading the iDronam GCS's own bundled code (docs/IDRONAM_NOTES.md). The
connection is exactly what iDronam does; re-implementing it here keeps the
Studio one Python process with no Node runtime, ffmpeg.exe or npm tree:

  telemetry  TCP to <ip>:14550, MAVLink v2, GCS identity sysid 255 / compid 1;
             REQUEST_DATA_STREAM(all) + SET_MESSAGE_INTERVAL on connect, then
             a 1 Hz GCS heartbeat (studio/mavlink.py is the codec)
  video      rtsp://<ip>:10000/drone_cam over TCP, decoded by OpenCV's bundled
             FFmpeg (the bridge spawned ffmpeg.exe against the same URL)

Neither needs iDronam running, and both are safe alongside it.

A take is frames/NNNNNN.jpg + frame_timestamps.json + gps_data.json +
session_meta.json - what vggt_reconstruct --from-recording and the Studio's
session list already read for phone takes - so a drone take gets the same
session card, viewer and Reconstruct button. drone_telemetry.json keeps
every telemetry message received during the take on top of that.

INDOOR / OUTDOOR
Latched when a take starts. Outdoor records the drone's fused
GLOBAL_POSITION_INT fixes (only while GPS_RAW_INT reports a 3D fix or
better) into gps_data.json, and server.py reconstructs it with --gps-mode
global (metric, east-north-up). Indoor writes an empty gps_data.json and is
reconstructed vision-only: indoor GPS is multipath noise that would drag
the Sim(3) fit rather than anchor it.

LENS
The drone's camera is wide-angle/fisheye, which VGGT - a pinhole-camera
model - cannot represent: straight walls bend and right angles open up. A
one-time checkerboard calibration (camera_calib.py, Drone tab -> Camera
calibration) is kept in data/drone_camera.json. Every take carries it,
scaled to the recorded size, as camera_intrinsics.json, and vggt_reconstruct
remaps the frames to a pinhole camera before VGGT sees them. The drone is
also asked for its own camera description over MAVLink (CAMERA_INFORMATION /
VIDEO_STREAM_INFORMATION) and whatever it answers is recorded - but that is
at best a focal length, never the distortion, and many drones do not
answer at all, which is why the checkerboard path exists.

TIMESTAMPS
Frames and GPS fixes are both stamped with this PC's clock on arrival, so
they share one clock with no drone/PC time sync. The cost is that the RTSP
pipeline's latency (typically a few hundred ms) makes every frame late
relative to GPS by a roughly constant amount - at 5 m/s, 300 ms is 1.5 m
along track. Measure it once (film a clock shown on the laptop) and set
drone.video_delay_ms; it is subtracted from every frame time.

WHY A WRITER THREAD (unlike phone_link.py's inline writes)
The phone can spool when the Studio falls behind; an RTSP stream cannot -
whatever is not read in time is lost at the source. So the grabber thread
never blocks on disk: frames go through a bounded queue to a writer, and a
frame that finds the queue full is counted in frames_dropped rather than
stalling the stream for every frame after it.
"""
import json
import math
import os
import queue
import shutil
import socket
import threading
import time
from collections import deque

import cv2
import numpy as np

from .. import camera_model
from . import mavlink
from .camera_calib import MIN_VIEWS as CALIB_MIN_VIEWS
from .camera_calib import TARGET_VIEWS as CALIB_TARGET_VIEWS
from .camera_calib import CalibrationSession

GCS_SYSID, GCS_COMPID = 255, 1          # the identity iDronam uses
MAV_RECONNECT_S = 3.0
MAV_SILENCE_S = 10.0                    # an autopilot streams several msgs/s; silence this long = dead link
VIDEO_RETRY_S = 3.0
VIDEO_TIMEOUT_MS = 5000
PREVIEW_FPS = 12
PREVIEW_LONG_SIDE = 960
WRITE_QUEUE_FRAMES = 90                 # ~3 s at 30 fps of record-size frames
# Rates asked for on connect: the capture-bridge's 4 Hz for everything,
# except position and attitude at 10 Hz - at a 5 m/s survey speed 4 Hz GPS
# leaves 1.25 m between fixes, 10 Hz 0.5 m, under the 1 m accuracy target.
MESSAGE_RATES_HZ = {"GLOBAL_POSITION_INT": 10, "ATTITUDE": 10, "GPS_RAW_INT": 4,
                    "VFR_HUD": 4, "SYS_STATUS": 4, "BATTERY_STATUS": 4}
CONNECTION_KEYS = ("enabled", "ip", "mavlink_port", "video_url")

AUTOPILOT_NAMES = {mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA: "ArduPilot", mavlink.MAV_AUTOPILOT_PX4: "PX4"}
COPTER_TYPES = {2, 3, 4, 13, 14, 15, 29}   # MAV_TYPE quad/coax/heli/hexa/octo/tri/dodeca
COPTER_MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER",
                6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
                15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
                20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW",
                24: "ZIGZAG", 25: "SYSTEMID", 26: "AUTOROTATE", 27: "AUTO_RTL"}
GPS_FIX_NAMES = ("no GPS", "no fix", "2D fix", "3D fix", "DGPS", "RTK float", "RTK fixed",
                 "static", "PPP")


def video_source(cfg):
    """drone.video_url with {ip} filled in; "" when it needs an IP that is not set."""
    url = (cfg.get("video_url") or "").strip()
    ip = (cfg.get("ip") or "").strip()
    if "{ip}" in url:
        return url.replace("{ip}", ip) if ip else ""
    return url


def fit_long_side(img, long_side):
    if not long_side:
        return img
    h, w = img.shape[:2]
    s = long_side / max(h, w)
    if s >= 1.0:
        return img
    return cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)


def new_drone_session_id(root):
    base = time.strftime("%Y%m%d-%H%M%S") + "-drone"
    sid, n = base, 1
    while os.path.exists(os.path.join(root, sid)):
        n += 1
        sid = "%s-%d" % (base, n)
    return sid


def _open_capture(src):
    """OpenCV's bundled FFmpeg, RTSP over TCP like the capture-bridge's
    `ffmpeg -rtsp_transport tcp` (RTP over UDP loses packets - smeared
    frames - on any lossy WiFi link). OPENCV_FFMPEG_CAPTURE_OPTIONS is only
    read while opening, and is put back right after so it does not leak into
    the reconstruction subprocesses jobs.py spawns with this process's env."""
    key = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
    rtsp = src.lower().startswith("rtsp")
    prev = os.environ.get(key)
    if rtsp:
        os.environ[key] = "rtsp_transport;tcp"
    try:
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS,
                                cv2.CAP_PROP_READ_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS])
    finally:
        if rtsp:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def vehicle_state(t):
    """The latest telemetry messages -> the handful of numbers the Studio shows."""
    s = {}
    hb = t.get("HEARTBEAT")
    if hb:
        s["armed"] = bool(hb["base_mode"] & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        s["autopilot"] = AUTOPILOT_NAMES.get(hb["autopilot"], "autopilot %d" % hb["autopilot"])
        if hb["autopilot"] == mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA and hb["type"] in COPTER_TYPES:
            s["flight_mode"] = COPTER_MODES.get(hb["custom_mode"], "mode %d" % hb["custom_mode"])
        else:
            s["flight_mode"] = "mode %d" % hb["custom_mode"]
    g = t.get("GPS_RAW_INT")
    if g:
        s["gps_fix"] = g["fix_type"]
        s["gps_fix_name"] = GPS_FIX_NAMES[g["fix_type"]] if g["fix_type"] < len(GPS_FIX_NAMES) else str(g["fix_type"])
        s["satellites"] = g["satellites_visible"] if g["satellites_visible"] != 255 else None
        s["h_acc_m"] = g["h_acc"] / 1e3 if g["h_acc"] else None
        s["hdop"] = g["eph"] / 100 if g["eph"] != 65535 else None
    p = t.get("GLOBAL_POSITION_INT")
    if p and (p["lat"] or p["lon"]):
        s["lat"], s["lon"] = p["lat"] / 1e7, p["lon"] / 1e7
        s["alt_msl_m"] = p["alt"] / 1e3
        s["rel_alt_m"] = p["relative_alt"] / 1e3
        if p["hdg"] != 65535:
            s["heading"] = p["hdg"] / 100
    a = t.get("ATTITUDE")
    if a:
        s["roll"], s["pitch"], s["yaw"] = (float(np.degrees(a[k])) for k in ("roll", "pitch", "yaw"))
    v = t.get("VFR_HUD")
    if v:
        s["groundspeed"], s["climb"] = v["groundspeed"], v["climb"]
        s.setdefault("heading", v["heading"])
    b, ss = t.get("BATTERY_STATUS"), t.get("SYS_STATUS")
    if b and b["battery_remaining"] >= 0:
        s["battery_pct"] = b["battery_remaining"]
    elif ss and ss["battery_remaining"] >= 0:
        s["battery_pct"] = ss["battery_remaining"]
    if ss and ss["voltage_battery"] not in (0, 65535):
        s["voltage"] = ss["voltage_battery"] / 1e3
    elif b:
        cells = [c for c in b["voltages"] if c not in (0, 65535)]
        if cells:
            s["voltage"] = sum(cells) / 1e3
    if ss and ss["current_battery"] != -1:
        s["current"] = ss["current_battery"] / 100
    elif b and b["current_battery"] != -1:
        s["current"] = b["current_battery"] / 100
    return s


class DroneTake:
    """One recording. push() is called from the video thread, add_gps() /
    add_telemetry() from the MAVLink thread; the writer thread owns the disk."""

    def __init__(self, root, session_id, cfg, camera=None, camera_reported=None):
        self.id = session_id
        self.dir = os.path.join(root, session_id)
        self.frames_dir = os.path.join(self.dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.mode = "outdoor" if cfg.get("mode") == "outdoor" else "indoor"
        self.long_side = int(cfg.get("record_long_side") or 0)
        self.jpeg_quality = int(cfg.get("jpeg_quality") or 90)
        self.delay_s = float(cfg.get("video_delay_ms") or 0) / 1e3
        self.params = {k: cfg.get(k) for k in ("mode", "record_long_side", "jpeg_quality", "video_delay_ms")}
        self.params["video_source"] = video_source(cfg)
        self.camera = camera                    # lens profile at the stream's native size, or None
        self.camera_reported = camera_reported
        self.created_wall = time.time()
        self.created = time.monotonic()

        self.frame_t = []                   # PC wall clock per written frame, video_delay_ms applied
        self.gps = []                       # (t, lat, lon, alt, acc)
        self.telemetry = []                 # (t, message name, fields)
        self.size = None
        self.source_size = None
        self.bytes = 0
        self.dropped = 0
        self.write_error = None
        self.stopping = False
        self._recent = deque(maxlen=90)
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=WRITE_QUEUE_FRAMES)
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="drone-writer")
        self._writer.start()

    # ------------------------------------------------------------ ingest --

    def push(self, t_wall, frame):
        """Resized here rather than in the writer so the queue holds
        record-size frames (one 4K frame is 25 MB)."""
        if self.stopping:
            return
        self.source_size = (frame.shape[1], frame.shape[0])
        try:
            self._q.put_nowait((t_wall - self.delay_s, fit_long_side(frame, self.long_side)))
        except queue.Full:
            self.dropped += 1

    def _write_loop(self):
        params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        while True:
            item = self._q.get()
            if item is None:
                return
            t, img = item
            ok, buf = cv2.imencode(".jpg", img, params)
            if not ok:
                self.dropped += 1
                continue
            idx = len(self.frame_t)
            try:
                with open(os.path.join(self.frames_dir, "%06d.jpg" % idx), "wb") as f:
                    f.write(buf.tobytes())
            except OSError as e:
                self.write_error = str(e)
                self.dropped += 1
                continue
            with self._lock:
                self.frame_t.append(t)
                self.size = (img.shape[1], img.shape[0])
                self.bytes += buf.size
            self._recent.append(time.monotonic())

    def add_telemetry(self, t_wall, name, fields):
        with self._lock:
            if not self.stopping:
                self.telemetry.append((t_wall, name, fields))

    def add_gps(self, t_wall, lat, lon, alt, acc):
        with self._lock:
            if not self.stopping:
                self.gps.append((t_wall, lat, lon, alt, acc))

    # ----------------------------------------------------------- readout --

    def live_fps(self):
        r = list(self._recent)
        if len(r) < 2 or time.monotonic() - r[-1] > 2.0:
            return 0.0
        return (len(r) - 1) / max(r[-1] - r[0], 1e-6)

    def summary(self):
        with self._lock:
            n = len(self.frame_t)
            span = self.frame_t[-1] - self.frame_t[0] if n > 1 else 0.0
            return {
                "id": self.id, "mode": self.mode, "frames": n,
                "fps": round(self.live_fps(), 1), "duration_s": round(span, 1),
                "elapsed_s": round(time.monotonic() - self.created, 1),
                "mb": round(self.bytes / 1e6, 1), "dropped": self.dropped,
                "queued": self._q.qsize(), "gps_fixes": len(self.gps), "size": self.size,
                "write_error": self.write_error,
            }

    # ---------------------------------------------------------- finalize --

    def finish(self, reason, vehicle=None):
        """Drains the writer, then writes the JSON sidecars. None if no frame
        was ever written."""
        with self._lock:
            self.stopping = True
        self._q.put(None)
        self._writer.join()
        if not self.frame_t:
            return None

        t0 = self.frame_t[0]
        rel = lambda t: round(t - t0, 6)

        def dump(name, obj):
            with open(os.path.join(self.dir, name), "w", newline="\n") as f:
                json.dump(obj, f)

        dump("frame_timestamps.json", [rel(t) for t in self.frame_t])
        dump("gps_data.json", [
            {"timestamp": rel(t), "latitude_deg": lat, "longitude_deg": lon,
             "altitude_m": alt, "accuracy_m": acc}
            for t, lat, lon, alt, acc in self.gps] if self.mode == "outdoor" else [])
        dump("drone_telemetry.json", {
            "t0_wall": t0,
            "note": "t is seconds from the first frame, on the same PC clock as "
                    "frame_timestamps.json. Fields are raw MAVLink units.",
            "samples": [dict(fields, t=rel(t), msg=name) for t, name, fields in self.telemetry],
        })

        dts = np.diff(np.array(self.frame_t, dtype=np.float64))
        span = float(self.frame_t[-1] - t0)
        med = float(np.median(dts)) if len(dts) else 0.0
        gaps = int((dts > 2.5 * med).sum()) if med > 0 else 0
        n = len(self.frame_t)
        dump("flight_config.json", {
            "fps": round(1.0 / med, 3) if med > 0 else 0.0,
            "n_frames": n,
            "total_time_s": round(span, 3),
            "gps_noise_std_m": None,
            "note": "fps is the MEASURED median frame interval. Use "
                    "frame_timestamps.json for per-frame timing.",
        })
        cam_meta, cam_warning = None, None
        if self.camera is not None and self.size:
            q = camera_model.scaled(self.camera, *self.size)
            if q is None:
                cam_warning = ("lens calibration is %sx%s but this take is %dx%d (a different aspect) "
                               "- not applied" % (self.camera.get("width"), self.camera.get("height"), *self.size))
            else:
                dump("camera_intrinsics.json",
                     dict(q, source="drone lens calibration: %s" % (self.camera.get("source") or "?")))
                cam_meta = camera_model.summary(q)
        meta = {
            "id": self.id,
            "origin": "drone",
            "drone_mode": self.mode,
            "reason": reason,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_wall)),
            "params": self.params,
            "frames_received": n,
            "frames_reported_sent": None,
            "frames_dropped": self.dropped,
            "complete": self.dropped == 0,
            "resolution": list(self.size) if self.size else None,
            "source_resolution": list(self.source_size) if self.source_size else None,
            "duration_s": round(span, 3),
            "fps_median": round(1.0 / med, 2) if med > 0 else 0.0,
            "fps_mean": round((n - 1) / span, 2) if span > 0 else 0.0,
            "frame_gaps": gaps,
            "non_monotonic_timestamps": int((dts < 0).sum()),
            "bytes": self.bytes,
            "imu_samples": 0,
            "gps_fixes": len(self.gps) if self.mode == "outdoor" else 0,
            "telemetry_samples": len(self.telemetry),
            "vehicle": vehicle,
            "camera": cam_meta,
            "camera_warning": cam_warning,
            "camera_reported": self.camera_reported or None,
            "write_error": self.write_error,
        }
        dump("session_meta.json", meta)
        return meta


class DroneLink:
    def __init__(self, sessions_root, cfg, on_session_finalized=None, camera_path=None, calib_root=None):
        self.root = sessions_root
        os.makedirs(self.root, exist_ok=True)
        self.on_session_finalized = on_session_finalized
        self.camera_path = camera_path          # lens profile (camera_model JSON) for this drone's camera
        self.calib_root = calib_root or os.path.join(os.path.dirname(os.path.abspath(sessions_root)), "drone_calib")
        self.camera = None
        self.camera_info = None                 # camera_model.summary(self.camera), cached
        self._preview_maps = None
        self.calib = None                       # CalibrationSession while capturing checkerboard views
        self.camera_reported = {}               # CAMERA_INFORMATION / VIDEO_STREAM_INFORMATION from the drone
        p = camera_model.load_profile(camera_path) if camera_path else None
        self._set_camera(p if p is not None and p.get("width") and p.get("height") else None)

        self._cfg = dict(cfg)
        self._gen = 0                       # bumped whenever a connection setting changes
        self._cfg_cond = threading.Condition()
        self._lock = threading.RLock()
        self.events = deque(maxlen=200)

        self.mav_connected = False
        self.mav_target = None
        self.mav_error = None
        self.mav_last_rx = None
        self._rx_times = deque(maxlen=500)
        self.vehicle = None                 # (sysid, compid) of the autopilot
        self.telem = {}                     # message name -> latest fields

        self.video_connected = False
        self.video_error = None
        self.video_size = None
        self._vt = deque(maxlen=60)

        self.latest_jpeg = None
        self.latest_seq = 0
        self._frame_cond = threading.Condition()

        self.active = None
        self.finishing = {}                 # take id -> DroneTake being written out
        self.last_finalized = None

    # ------------------------------------------------------------ config --

    def start(self):
        threading.Thread(target=self._mav_loop, daemon=True, name="drone-mavlink").start()
        threading.Thread(target=self._video_loop, daemon=True, name="drone-video").start()

    def configure(self, cfg):
        """New drone settings. Changing a connection setting reconnects both
        links; anything else (mode, JPEG quality, ...) applies to the next take."""
        with self._cfg_cond:
            changed = any(self._cfg.get(k) != cfg.get(k) for k in CONNECTION_KEYS)
            self._cfg = dict(cfg)
            if changed:
                self._gen += 1
                self._cfg_cond.notify_all()

    def _current(self):
        with self._cfg_cond:
            return self._gen, dict(self._cfg)

    def _stale(self, gen):
        return self._gen != gen

    def _wait_change(self, gen, timeout):
        with self._cfg_cond:
            self._cfg_cond.wait_for(lambda: self._gen != gen, timeout)

    def _event(self, text):
        self.events.append((time.strftime("%H:%M:%S"), text))
        print("[drone] %s" % text, flush=True)

    def _set_error(self, kind, msg, log=True):
        """Logged only when it changes, so a drone that is simply switched off
        does not fill the event log with one line per retry."""
        attr = kind + "_error"
        with self._lock:
            changed = msg != getattr(self, attr)
            setattr(self, attr, msg)
        if msg and changed and log:
            self._event(msg)

    # --------------------------------------------------------- telemetry --

    def _mav_loop(self):
        while True:
            gen, cfg = self._current()
            ip = (cfg.get("ip") or "").strip()
            if not cfg.get("enabled") or not ip:
                self._set_error("mav", None)
                self._wait_change(gen, None)
                continue
            try:
                port = int(cfg.get("mavlink_port") or 14550)
                sock = socket.create_connection((ip, port), timeout=3.0)
            except (OSError, ValueError) as e:
                self._set_error("mav", "telemetry: cannot reach %s:%s - %s" % (ip, cfg.get("mavlink_port"), e))
                self._wait_change(gen, MAV_RECONNECT_S)
                continue
            reason = self._serve_mav(sock, gen, "%s:%d" % (ip, port))
            if reason is not None:
                self._set_error("mav", "telemetry lost: %s" % reason, log=False)
            self._wait_change(gen, MAV_RECONNECT_S)

    def _serve_mav(self, sock, gen, target):
        parser = mavlink.Parser()
        seq = 0

        def send(name, **fields):
            nonlocal seq
            sock.sendall(mavlink.encode(name, seq, GCS_SYSID, GCS_COMPID, **fields))
            seq = (seq + 1) & 0xFF

        with self._lock:
            self.mav_connected = True
            self.mav_target = target
            self.mav_error = None
            self.mav_last_rx = None
            self.vehicle = None
            self.telem = {}
            self.camera_reported = {}
        self._event("telemetry connected: %s (MAVLink over TCP)" % target)
        reason = None
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(0.5)
            self._request_streams(send, 0, 0)
            last_hb = 0.0
            last_rx = time.monotonic()
            cam_requests, cam_req_at = 0, 0.0
            while not self._stale(gen):
                now = time.monotonic()
                if now - last_hb >= 1.0:
                    send("HEARTBEAT", type=mavlink.MAV_TYPE_GCS,
                         autopilot=mavlink.MAV_AUTOPILOT_INVALID, mavlink_version=3)
                    last_hb = now
                if (self.vehicle is not None and not self.camera_reported and cam_requests < 3
                        and now - cam_req_at > 5.0):
                    self._request_camera_info(send, self.vehicle[0])
                    cam_requests += 1
                    cam_req_at = now
                if now - last_rx > MAV_SILENCE_S:
                    reason = "no data for %d s" % MAV_SILENCE_S
                    break
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    reason = "the drone closed the connection"
                    break
                last_rx = time.monotonic()
                for msg in parser.feed(data):
                    self._on_mav(msg, send)
        except OSError as e:
            reason = str(e) or e.__class__.__name__
        finally:
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                self.mav_connected = False
        self._event("telemetry disconnected (%s)" % (reason or "settings changed"))
        return reason

    def _request_streams(self, send, sysid, compid):
        # Both, like the capture-bridge: older ArduPilot streams on
        # REQUEST_DATA_STREAM, PX4 / newer ArduPilot want per-message intervals.
        send("REQUEST_DATA_STREAM", target_system=sysid, target_component=compid,
             req_stream_id=mavlink.MAV_DATA_STREAM_ALL, req_message_rate=4, start_stop=1)
        for name, hz in MESSAGE_RATES_HZ.items():
            send("COMMAND_LONG", target_system=sysid, target_component=compid,
                 command=mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                 param1=mavlink.IDS[name], param2=1e6 / hz)

    def _request_camera_info(self, send, sysid):
        # To every component: a camera is its own component (100+), not the autopilot.
        for name in ("CAMERA_INFORMATION", "VIDEO_STREAM_INFORMATION"):
            send("COMMAND_LONG", target_system=sysid, target_component=mavlink.MAV_COMP_ID_ALL,
                 command=mavlink.MAV_CMD_REQUEST_MESSAGE, param1=mavlink.IDS[name])

    def _on_camera_message(self, msg):
        f = {k: v for k, v in msg.fields.items() if k not in ("cam_definition_uri", "uri")}
        f["compid"] = msg.compid
        with self._lock:
            first = msg.name not in self.camera_reported
            self.camera_reported[msg.name] = f
        if not first:
            return
        if msg.name == "CAMERA_INFORMATION":
            fov = ""
            if f["focal_length"] > 0 and f["sensor_size_h"] > 0:
                fov = " = %.0f deg across if it were a pinhole lens" % math.degrees(
                    2 * math.atan(f["sensor_size_h"] / 2 / f["focal_length"]))
            self._event("drone camera reports: %s %s, focal length %.2f mm, sensor %.2f x %.2f mm, %dx%d%s"
                        % (f["vendor_name"] or "?", f["model_name"] or "?", f["focal_length"],
                           f["sensor_size_h"], f["sensor_size_v"], f["resolution_h"], f["resolution_v"], fov))
        else:
            self._event("drone video stream reports: %s %dx%d @ %.0f fps, %d deg horizontal field of view"
                        % (f["name"] or "stream", f["resolution_h"], f["resolution_v"], f["framerate"], f["hfov"]))

    def _on_mav(self, msg, send):
        f = msg.fields
        if msg.name in ("CAMERA_INFORMATION", "VIDEO_STREAM_INFORMATION"):
            self._on_camera_message(msg)
            return
        if msg.name == "HEARTBEAT":
            # Gimbals, cameras and other ground stations heartbeat on the
            # same link; only an autopilot's heartbeat identifies the vehicle.
            if f["autopilot"] == mavlink.MAV_AUTOPILOT_INVALID or f["type"] == mavlink.MAV_TYPE_GCS:
                return
            if self.vehicle is None:
                self.vehicle = (msg.sysid, msg.compid)
                self._event("vehicle found: system %d, %s" % (
                    msg.sysid, AUTOPILOT_NAMES.get(f["autopilot"], "autopilot %d" % f["autopilot"])))
                # Ask again, addressed to it: some autopilots ignore the broadcast target.
                self._request_streams(send, msg.sysid, msg.compid)
        if self.vehicle is not None and msg.sysid != self.vehicle[0]:
            return
        now = time.time()
        with self._lock:
            self.telem[msg.name] = f
            self.mav_last_rx = time.monotonic()
            self._rx_times.append(self.mav_last_rx)
        if msg.name == "STATUSTEXT":
            self._event("drone says: %s" % f["text"])

        take = self.active
        if take is None:
            return
        take.add_telemetry(now, msg.name, f)
        if msg.name == "GLOBAL_POSITION_INT" and take.mode == "outdoor" and (f["lat"] or f["lon"]):
            gps = self.telem.get("GPS_RAW_INT")
            if gps is None or gps["fix_type"] >= 3:
                take.add_gps(now, f["lat"] / 1e7, f["lon"] / 1e7, f["alt"] / 1e3,
                             gps["h_acc"] / 1e3 if gps and gps["h_acc"] else None)

    # ------------------------------------------------------------- video --

    def _video_loop(self):
        while True:
            gen, cfg = self._current()
            src = video_source(cfg)
            if not cfg.get("enabled") or not src:
                self._set_error("video", None)
                self._wait_change(gen, None)
                continue
            cap = _open_capture(src)
            if cap is None:
                self._set_error("video", "video: cannot open %s" % src)
                self._wait_change(gen, VIDEO_RETRY_S)
                continue
            reason = self._serve_video(cap, gen, src)
            if reason is not None:
                self._set_error("video", "video lost: %s" % reason, log=False)
            self._wait_change(gen, 1.0)

    def _serve_video(self, cap, gen, src):
        # A local file stands in for the drone in testing: paced to its own
        # frame rate and looped, so it behaves like a live stream.
        is_file = os.path.isfile(src)
        period = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30.0) if is_file else 0.0
        with self._lock:
            self.video_connected = True
            self.video_error = None
            self._vt.clear()
        self._event("video connected: %s%s" % (src, " (local file, looped)" if is_file else ""))
        reason = None
        next_t = time.monotonic()
        last_preview = 0.0
        try:
            while not self._stale(gen):
                ok, frame = cap.read()
                if not ok and is_file and cap.set(cv2.CAP_PROP_POS_FRAMES, 0):
                    ok, frame = cap.read()
                if not ok or frame is None:
                    reason = "stream ended or timed out"
                    break
                if is_file:
                    next_t += period
                    d = next_t - time.monotonic()
                    if d > 0:
                        time.sleep(d)
                    elif d < -1.0:
                        next_t = time.monotonic()
                t_wall = time.time()
                mono = time.monotonic()
                with self._lock:
                    self.video_size = (frame.shape[1], frame.shape[0])
                    self._vt.append(mono)
                take = self.active
                if take is not None:
                    take.push(t_wall, frame)
                calib = self.calib
                if calib is not None and calib.active:
                    calib.offer(frame)
                if mono - last_preview >= 1.0 / PREVIEW_FPS:
                    last_preview = mono
                    pv = fit_long_side(frame, PREVIEW_LONG_SIDE)
                    if calib is not None and calib.active:
                        pv = self._draw_calibration(pv, calib)
                    elif self._cfg.get("preview_undistort") and self.camera is not None:
                        pv = self._undistort_preview(pv)
                    ok, buf = cv2.imencode(".jpg", pv, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        self._set_preview(buf.tobytes())
        except cv2.error as e:
            reason = str(e)
        finally:
            cap.release()
            with self._lock:
                self.video_connected = False
        self._event("video disconnected (%s)" % (reason or "settings changed"))
        return reason

    def _set_preview(self, jpeg):
        with self._frame_cond:
            self.latest_jpeg = jpeg
            self.latest_seq += 1
            self._frame_cond.notify_all()

    def wait_preview(self, after_seq, timeout=2.0):
        with self._frame_cond:
            if self.latest_seq <= after_seq:
                self._frame_cond.wait(timeout)
            return self.latest_seq, self.latest_jpeg

    def _draw_calibration(self, pv, calib):
        pv = pv.copy()          # never draw on a frame a take or the detector may still hold
        c = calib.last_corners
        if c is not None and calib.last_seen and time.monotonic() - calib.last_seen < 0.6 and calib.size:
            s = pv.shape[1] / calib.size[0]
            cv2.drawChessboardCorners(pv, calib.board, ((c + 0.5) * s - 0.5).reshape(-1, 1, 2).astype(np.float32),
                                      True)
        cv2.putText(pv, "calibrating: %d/%d views" % (len(calib.views), CALIB_TARGET_VIEWS), (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 230, 255), 2, cv2.LINE_AA)
        return pv

    def _undistort_preview(self, pv):
        h, w = pv.shape[:2]
        cam, maps = self.camera, self._preview_maps
        if maps is None or maps[0] != (w, h):
            q = camera_model.scaled(cam, w, h)
            if q is None:
                return pv
            m1, m2, _k = camera_model.undistort_maps(q)
            maps = self._preview_maps = ((w, h), m1, m2)
        return cv2.remap(pv, maps[1], maps[2], cv2.INTER_LINEAR)

    # ------------------------------------------------------- calibration --

    def _set_camera(self, p):
        self.camera = p
        self.camera_info = camera_model.summary(p) if p is not None else None
        self._preview_maps = None

    def start_calibration(self, cols, rows):
        """Starts (or resumes) capturing checkerboard views from the live video."""
        cols, rows = int(cols), int(rows)
        with self._lock:
            if not self.video_connected:
                return False, "no video from the drone"
            calib = self.calib
            if (calib is not None and not calib.active and calib.board == (cols, rows)
                    and len(calib.views) < CALIB_TARGET_VIEWS):
                calib.resume()
                return True, None
            if calib is not None:
                calib.stop()
            self.calib = CalibrationSession(cols, rows, os.path.join(self.calib_root, time.strftime("%Y%m%d-%H%M%S")))
        self._event("lens calibration: capturing %dx%d-corner checkerboard views -> %s"
                    % (cols, rows, self.calib.save_dir))
        return True, None

    def stop_calibration(self):
        calib = self.calib
        if calib is None:
            return False, "not capturing"
        calib.stop()
        calib.status = "%d views captured" % len(calib.views)
        return True, None

    def discard_calibration(self):
        calib, self.calib = self.calib, None
        if calib is not None:
            calib.stop()
        return True, None

    def solve_calibration(self):
        calib = self.calib
        if calib is None:
            return False, "capture some checkerboard views first"
        if len(calib.views) < CALIB_MIN_VIEWS:
            return False, "need at least %d views, have %d" % (CALIB_MIN_VIEWS, len(calib.views))
        calib.stop()
        try:
            prof = calib.solve()
        except (ValueError, cv2.error, np.linalg.LinAlgError) as e:
            return False, "calibration failed: %s" % e
        if self.camera_path:
            if os.path.exists(self.camera_path):
                shutil.copy(self.camera_path, self.camera_path + ".bak")
            with open(self.camera_path, "w") as f:
                json.dump(prof, f, indent=2)
        self._set_camera(prof)
        self.calib = None
        s = self.camera_info
        self._event("lens calibrated: %s model, %.0f x %.0f deg field of view, RMS %.2f px over %d views%s"
                    % (s["model"], s.get("hfov") or 0, s.get("vfov") or 0, prof["rms_px"], prof["views"],
                       " -> %s" % self.camera_path if self.camera_path else ""))
        return True, s

    def remove_camera_profile(self):
        if self.camera_path and os.path.exists(self.camera_path):
            os.replace(self.camera_path, self.camera_path + ".bak")
        self._set_camera(None)
        self._event("lens calibration removed (a copy is kept as %s.bak)" % os.path.basename(self.camera_path or "?"))
        return True, None

    # --------------------------------------------------------- recording --

    def start_recording(self):
        """Returns (ok, session_id_or_error)."""
        _gen, cfg = self._current()
        with self._lock:
            if self.active is not None:
                return False, "already recording %s" % self.active.id
            if not self.video_connected:
                return False, "no video from the drone"
            take = DroneTake(self.root, new_drone_session_id(self.root), cfg, camera=self.camera,
                             camera_reported=dict(self.camera_reported) or None)
            self.active = take
            gps = self.telem.get("GPS_RAW_INT") if self.mav_connected else None
        note = ""
        if take.mode == "outdoor" and not (gps and gps["fix_type"] >= 3):
            note = " - WARNING: no 3D GPS fix yet; fixes are only recorded once there is one"
        if self.camera is None:
            note += " - lens not calibrated, the reconstruction will be warped"
        self._event("take %s started (%s mode)%s" % (take.id, take.mode, note))
        return True, take.id

    def stop_recording(self):
        with self._lock:
            take = self.active
            if take is None:
                return False, "not recording"
            self.active = None
            self.finishing[take.id] = take
        threading.Thread(target=self._finish, args=(take,), daemon=True, name="drone-finish").start()
        return True, take.id

    def _vehicle_info(self):
        with self._lock:
            if self.vehicle is None:
                return None
            st = vehicle_state(self.telem)
            return {"sysid": self.vehicle[0], "compid": self.vehicle[1],
                    "autopilot": st.get("autopilot"), "flight_mode": st.get("flight_mode")}

    def _finish(self, take):
        try:
            try:
                meta = take.finish("stopped from Studio", vehicle=self._vehicle_info())
            except Exception as e:                      # noqa: BLE001 - never lose a take silently
                self._event("take %s could not be finalized: %s" % (take.id, e))
                return
            if meta is None:
                shutil.rmtree(take.dir, ignore_errors=True)
                self._event("take %s ended with no frames" % take.id)
                return
            self.last_finalized = meta
            drops = " - WARNING: %d frames dropped" % meta["frames_dropped"] if meta["frames_dropped"] else ""
            self._event("take %s saved: %d frames, %.1f s, %.1f fps, %d GPS fixes (%s)%s"
                        % (take.id, meta["frames_received"], meta["duration_s"], meta["fps_mean"],
                           meta["gps_fixes"], take.mode, drops))
            if take.mode == "outdoor" and not meta["gps_fixes"]:
                self._event("take %s is Outdoor but recorded no GPS fixes (no telemetry or no 3D fix) "
                            "- it will be reconstructed vision-only" % take.id)
            if self.on_session_finalized is not None:
                self.on_session_finalized(take.dir, meta)
        finally:
            with self._lock:
                self.finishing.pop(take.id, None)

    def busy_ids(self):
        """Takes whose directory is still being written."""
        with self._lock:
            ids = set(self.finishing)
            if self.active is not None:
                ids.add(self.active.id)
            return ids

    # ------------------------------------------------------------ readout --

    def snapshot(self):
        _gen, cfg = self._current()
        now = time.monotonic()
        with self._lock:
            vt = list(self._vt)
            fps = ((len(vt) - 1) / max(vt[-1] - vt[0], 1e-6)
                   if len(vt) > 1 and now - vt[-1] < 2.0 else 0.0)
            return {
                "enabled": bool(cfg.get("enabled")),
                "ip": (cfg.get("ip") or "").strip(),
                "mode": "outdoor" if cfg.get("mode") == "outdoor" else "indoor",
                "video_source": video_source(cfg),
                "telemetry": {
                    "connected": self.mav_connected,
                    "target": self.mav_target,
                    "error": self.mav_error,
                    "age_s": round(now - self.mav_last_rx, 1) if self.mav_last_rx else None,
                    "msg_rate": sum(1 for t in self._rx_times if now - t <= 2.0) / 2.0,
                    "vehicle": {"sysid": self.vehicle[0], "compid": self.vehicle[1]} if self.vehicle else None,
                },
                "video": {
                    "connected": self.video_connected,
                    "error": self.video_error,
                    "size": self.video_size,
                    "fps": round(fps, 1),
                },
                "vehicle_state": vehicle_state(self.telem) if self.mav_connected else {},
                "camera": self.camera_info,
                "calibration": self.calib.summary() if self.calib is not None else None,
                "camera_reported": dict(self.camera_reported),
                "recording": self.active.summary() if self.active is not None else None,
                "finishing": sorted(self.finishing),
                "last_finalized": self.last_finalized,
                "events": list(self.events)[-40:],
            }
