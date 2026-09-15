"""
The phone side of rtvio.studio: a TCP server the rtvioapk app connects to,
which can start and stop recordings remotely and writes each take to its
own session directory.

Protocol (stream/protocol.py): the studio greets with handshake version 2.
A v2-aware app then connects "armed" - camera preview running, nothing
recorded - and only streams frames between a START and a STOP command. It
reports its state about once a second in STATUS packets:

    armed      connected, idle, ready for START
    recording  capturing; frames are streaming (or spooling to the phone's
               storage if WiFi can't keep up - see StreamClient's spool)
    finishing  STOP received; the camera is off and the phone is uploading
               whatever spooled up during the take
    armed + last_completed=<id>
               every frame of <id> has been written to the socket

A session is only finalized (and handed to the reconstruction queue) on
that last signal, not on STOP - after STOP there can be minutes of spooled
frames still to come over a slow link.

A v1 app (older rtvioapk build) ignores the version, streams immediately and
never sends STATUS. That still records: frames that arrive with no session
open and no STATUS ever seen start a "legacy" session that ends when the
stream stops.

WHY FRAMES ARE WRITTEN INLINE
Every frame is written to disk synchronously on the socket thread. A ~100 KB
write to a local SSD is well under a millisecond, and doing it inline means
there is no queue that can overflow and silently drop frames. That is the
opposite trade from stream/recorder.py's bounded queue, deliberately: a live
pipeline must never stall on its recorder, but a capture whose whole purpose
is to use every frame must never drop one. If the disk really is too slow,
TCP backpressure slows the phone, and the phone spools instead of dropping.
"""
import json
import os
import shutil
import socket
import threading
import time
from collections import deque

import numpy as np

from ..stream import protocol

START_CONFIRM_TIMEOUT_S = 8.0   # phone must report "recording" this soon after START
RESUME_GRACE_S = 120.0          # a phone that drops mid-take may reconnect and keep uploading
LEGACY_IDLE_FINALIZE_S = 3.0    # v1 app: its auto-session ends this long after frames stop
SOCKET_IDLE_TIMEOUT_S = 15.0    # a v2 phone sends STATUS every second; silence this long = dead link
STOP_RESEND_S = 2.0


def new_session_id(root):
    base = time.strftime("%Y%m%d-%H%M%S")
    sid, n = base, 1
    while os.path.exists(os.path.join(root, sid)):
        n += 1
        sid = "%s-%d" % (base, n)
    return sid


class SessionWriter:
    """One take, written in the layout vggt_reconstruct.reconstruct_from_recording
    reads: frames/NNNNNN.jpg (bytes exactly as the phone encoded them) plus
    frame_timestamps.json etc., written at finalize()."""

    def __init__(self, root, session_id, params, origin):
        self.id = session_id
        self.dir = os.path.join(root, session_id)
        self.frames_dir = os.path.join(self.dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.params = dict(params or {})
        self.origin = origin                    # "studio" | "phone" | "legacy"
        self.created_wall = time.time()
        self.created = time.monotonic()
        self.confirmed = origin != "studio"     # studio-started takes wait for the phone's ack
        self.stop_requested_at = None
        self.stop_sent_at = None
        self.disconnected_at = None
        self.closed = False

        self.frame_ms = []                      # phone wall-clock capture time per frame
        self.gps = []                           # (wall_ms, lat, lon, alt, acc)
        self.imu = []                           # (t_ns, ax, ay, az, gx, gy, gz), boot clock
        self.intrinsics = None
        self.size = None
        self.bytes = 0
        self.non_monotonic = 0
        self.boot_to_wall = []                  # from STATUS clock pairs
        self.last_status = {}
        self.last_frame_at = None
        self._recent = deque(maxlen=120)        # receive times, for the live fps readout
        self._lock = threading.Lock()

    # ------------------------------------------------------------ ingest --

    def add_frame(self, pkt):
        with self._lock:
            if self.closed:
                return False
            idx = len(self.frame_ms)
            with open(os.path.join(self.frames_dir, "%06d.jpg" % idx), "wb") as f:
                f.write(pkt.jpeg)
            if self.frame_ms and pkt.timestamp_ms < self.frame_ms[-1]:
                self.non_monotonic += 1
            self.frame_ms.append(pkt.timestamp_ms)
            self.size = (pkt.width, pkt.height)
            self.bytes += len(pkt.jpeg)
            now = time.monotonic()
            self.last_frame_at = now
            self._recent.append(now)
            return True

    def add_imu(self, samples):
        with self._lock:
            if not self.closed:
                self.imu.extend((s.t_ns, *s.accel, *s.gyro) for s in samples)

    def add_gps(self, pkt):
        with self._lock:
            if not self.closed:
                self.gps.append((pkt.timestamp_ms, pkt.lat_deg, pkt.lon_deg,
                                 pkt.altitude_m, pkt.accuracy_m))

    def note_status(self, st):
        with self._lock:
            self.last_status = st
            # wall_ms / elapsed_ns are read back to back on the phone
            # (System.currentTimeMillis + SystemClock.elapsedRealtimeNanos),
            # which pins the IMU's boot clock to the frames' wall clock far
            # better than stream/clock.py's receive-time minimum filter can.
            if "wall_ms" in st and "elapsed_ns" in st:
                self.boot_to_wall.append(st["wall_ms"] / 1e3 - st["elapsed_ns"] / 1e9)

    # ----------------------------------------------------------- readout --

    @property
    def n_frames(self):
        return len(self.frame_ms)

    def live_fps(self):
        r = list(self._recent)
        if len(r) < 2 or time.monotonic() - r[-1] > 2.0:
            return 0.0
        return (len(r) - 1) / max(r[-1] - r[0], 1e-6)

    def summary(self):
        st = self.last_status
        span = (self.frame_ms[-1] - self.frame_ms[0]) / 1e3 if len(self.frame_ms) > 1 else 0.0
        return {
            "id": self.id,
            "origin": self.origin,
            "confirmed": self.confirmed,
            "stopping": self.stop_requested_at is not None,
            "frames": self.n_frames,
            "fps": round(self.live_fps(), 1),
            "capture_fps": round((self.n_frames - 1) / span, 1) if span > 0 else 0.0,
            "duration_s": round(span, 1),
            "mb": round(self.bytes / 1e6, 1),
            "size": self.size,
            "gps_fixes": len(self.gps),
            "imu_samples": len(self.imu),
            "phone_backlog": st.get("queued", 0),
            "phone_sent": st.get("sent"),
            "phone_captured": st.get("captured"),
            "phone_skipped": st.get("skipped"),
            "elapsed_s": round(time.monotonic() - self.created, 1),
        }

    # ---------------------------------------------------------- finalize --

    def finalize(self, reason, frames_sent=None):
        with self._lock:
            if self.closed:
                return None
            self.closed = True
        if not self.frame_ms:
            return None

        t0_ms = self.frame_ms[0]
        rel = lambda ms: round((ms - t0_ms) / 1e3, 6)
        b2w = float(np.median(self.boot_to_wall)) if self.boot_to_wall else None

        def dump(name, obj):
            with open(os.path.join(self.dir, name), "w", newline="\n") as f:
                json.dump(obj, f)

        dump("frame_timestamps.json", [rel(ms) for ms in self.frame_ms])
        dump("gps_data.json", [
            {"timestamp": rel(ms), "latitude_deg": lat, "longitude_deg": lon,
             "altitude_m": alt, "accuracy_m": acc}
            for ms, lat, lon, alt, acc in self.gps])
        if b2w is not None:
            imu_rows = [{"timestamp": round(t_ns / 1e9 + b2w - t0_ms / 1e3, 6),
                         "accel_body_xyz": [ax, ay, az], "gyro_body_xyz": [gx, gy, gz]}
                        for t_ns, ax, ay, az, gx, gy, gz in self.imu]
        else:
            imu_rows = [{"t_ns_boot": t_ns, "accel_body_xyz": [ax, ay, az],
                         "gyro_body_xyz": [gx, gy, gz]}
                        for t_ns, ax, ay, az, gx, gy, gz in self.imu]
        dump("imu_data.json", imu_rows)
        if self.intrinsics is not None:
            dump("camera_intrinsics.json", self.intrinsics)

        dts = np.diff(np.array(self.frame_ms, dtype=np.float64)) / 1e3
        span = float(dts.sum()) if len(dts) else 0.0
        med = float(np.median(dts)) if len(dts) else 0.0
        gaps = int((dts > 2.5 * med).sum()) if med > 0 else 0
        dump("flight_config.json", {
            "fps": round(1.0 / med, 3) if med > 0 else 0.0,
            "n_frames": self.n_frames,
            "total_time_s": round(span, 3),
            "gps_noise_std_m": None,
            "note": "fps is the MEASURED median frame interval. Use "
                    "frame_timestamps.json for per-frame timing.",
        })
        st = self.last_status
        meta = {
            "id": self.id,
            "origin": self.origin,
            "reason": reason,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_wall)),
            "params": self.params,
            "frames_received": self.n_frames,
            "frames_reported_sent": frames_sent,
            "complete": (frames_sent is None and self.origin == "legacy")
                        or (frames_sent is not None and frames_sent == self.n_frames),
            "frames_captured_by_phone": st.get("captured"),
            "frames_skipped_by_camera": st.get("skipped"),
            "resolution": list(self.size) if self.size else None,
            "duration_s": round(span, 3),
            "fps_median": round(1.0 / med, 2) if med > 0 else 0.0,
            "fps_mean": round((self.n_frames - 1) / span, 2) if span > 0 else 0.0,
            "frame_gaps": gaps,
            "non_monotonic_timestamps": self.non_monotonic,
            "bytes": self.bytes,
            "imu_samples": len(self.imu),
            "gps_fixes": len(self.gps),
            "boot_to_wall_s": b2w,
            "phone": {k: st.get(k) for k in ("model", "app", "android", "jpeg_quality",
                                             "fps_target", "resolution", "encode_ms")},
        }
        dump("session_meta.json", meta)
        return meta


class PhoneLink:
    def __init__(self, sessions_root, host="0.0.0.0", port=5555, on_session_finalized=None):
        self.root = sessions_root
        os.makedirs(self.root, exist_ok=True)
        self.host, self.port = host, port
        self.on_session_finalized = on_session_finalized

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._conn = None
        self.peer = None
        self.connected_since = None
        self.v2 = False                 # this connection has sent STATUS
        self.status = {}
        self.status_at = None
        self.intrinsics = None          # latest 0xFC packet; copied into each new session
        self.active = None
        self.last_finalized = None
        self.orphan_frames = 0
        self.events = deque(maxlen=200)
        self.listen_error = None

        self.latest_jpeg = None
        self.latest_seq = 0
        self._frame_cond = threading.Condition()

    # ----------------------------------------------------------- threads --

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True, name="phone-accept").start()
        threading.Thread(target=self._watchdog, daemon=True, name="phone-watchdog").start()

    def _event(self, text):
        self.events.append((time.strftime("%H:%M:%S"), text))
        print("[phone] %s" % text, flush=True)

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            self.listen_error = "cannot listen on %s:%d - %s" % (self.host, self.port, e)
            self._event(self.listen_error)
            return
        srv.listen(2)
        self._event("listening for the phone on %s:%d" % (self.host, self.port))
        while True:
            conn, addr = srv.accept()
            # One phone at a time. A second connection is almost always the
            # same phone auto-reconnecting after a WiFi blip before our end of
            # the old socket noticed it was dead - so the new one wins.
            with self._lock:
                old = self._conn
                self._conn = conn
                self.peer = "%s:%d" % addr
                self.connected_since = time.monotonic()
                self.v2 = False
                self.status = {}
                self.status_at = None
            if old is not None:
                self._close(old)
            threading.Thread(target=self._serve, args=(conn,), daemon=True,
                             name="phone-reader").start()

    @staticmethod
    def _close(conn):
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()

    def _serve(self, conn):
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(SOCKET_IDLE_TIMEOUT_S)
            conn.sendall(protocol.encode_handshake(protocol.STUDIO_PROTOCOL_VERSION))
            self._event("phone connected from %s" % self.peer)
            while True:
                header = protocol.recv_exact(conn, 1)[0]
                if header == protocol.HEADER_FRAME:
                    self._on_frame(protocol.read_frame(conn))
                elif header == protocol.HEADER_PREVIEW:
                    self._set_preview(protocol.read_frame(conn).jpeg)
                elif header == protocol.HEADER_IMU:
                    self._on_imu(protocol.read_imu(conn))
                elif header == protocol.HEADER_GPS:
                    self._on_gps(protocol.read_gps(conn))
                elif header == protocol.HEADER_INTRINSICS:
                    self._on_intrinsics(protocol.read_intrinsics(conn))
                elif header == protocol.HEADER_STATUS:
                    self._on_status(protocol.read_status(conn))
                elif header == protocol.HEADER_SESSION_BEGIN:
                    # One-shot bulk copy (Saved sessions -> Transfer), never
                    # mixed with live streaming - handle it and stop, rather
                    # than looping back to read another FRAME/IMU/... header
                    # that will never come on this connection.
                    self._receive_session_transfer(conn)
                    return
                else:
                    raise protocol.StreamClosed("unknown packet header 0x%02X" % header)
        except (protocol.StreamClosed, OSError) as e:
            reason = "timed out" if isinstance(e, socket.timeout) else str(e)
            self._event("phone disconnected (%s)" % reason)
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    self.peer = None
                    self.connected_since = None
                    if self.active is not None:
                        self.active.disconnected_at = time.monotonic()
                        if self.active.origin == "legacy":
                            self._finalize(self.active, "legacy stream ended")
            self._close(conn)

    def _receive_session_transfer(self, conn):
        """One HEADER_SESSION_BEGIN...HEADER_SESSION_END bulk copy: the
        app's Saved sessions -> Transfer action, RECORD LOCALLY's
        counterpart to a live stream. Writes to self.root/<session id>/,
        preserving the relative paths the phone sent - exactly
        frames/NNNNNN.jpg plus the *.json sidecars
        _load_recording_frames/load_gps_track_from_recording already read,
        so the result is immediately usable with --from-recording and shows
        up in Studio's session list the same as a live-recorded take. Same
        wire format rtvioapk/tools/mock_receiver.py --sessions-dir already
        validates end-to-end against the app.

        Raising protocol.StreamClosed here is caught by _serve's own
        try/except exactly like a malformed live-stream packet would be -
        no separate error handling needed."""
        session_id, file_count, total_bytes = protocol.read_session_begin(conn)
        dest_root = os.path.join(self.root, session_id)
        os.makedirs(dest_root, exist_ok=True)
        self._event("receiving transferred session '%s': %d files, %.1f MB"
                    % (session_id, file_count, total_bytes / 1e6))

        for _ in range(file_count):
            header = protocol.recv_exact(conn, 1)[0]
            if header != protocol.HEADER_SESSION_FILE:
                raise protocol.StreamClosed("expected a session file header, got 0x%02X" % header)
            rel_path, file_size = protocol.read_session_file_header(conn)
            # rel_path always uses '/' (the phone is Android); os.path.join
            # with a POSIX-style relative path still resolves on Windows.
            dest_path = os.path.join(dest_root, *rel_path.split("/"))
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                remaining = file_size
                while remaining:
                    chunk = conn.recv(min(remaining, 1 << 20))
                    if not chunk:
                        raise protocol.StreamClosed("peer closed mid-file (%s)" % rel_path)
                    f.write(chunk)
                    remaining -= len(chunk)

        end_header = protocol.recv_exact(conn, 1)[0]
        if end_header != protocol.HEADER_SESSION_END:
            raise protocol.StreamClosed("expected the transfer-end marker, got 0x%02X" % end_header)

        self._event("session '%s' transferred: %d files, %.1f MB -> %s"
                    % (session_id, file_count, total_bytes / 1e6, dest_root))
        meta = self._normalize_transferred_meta(dest_root, session_id, file_count, total_bytes)
        self.last_finalized = meta
        if self.on_session_finalized is not None:
            threading.Thread(target=self.on_session_finalized, args=(dest_root, meta),
                             daemon=True).start()

    @staticmethod
    def _normalize_transferred_meta(dest_root, session_id, file_count, total_bytes):
        """LocalSessionRecorder.kt writes its own session_meta.json in a
        different shape (frames_written/total_time_s/fps - see its
        finish()) than SessionWriter.finalize's live-recording one
        (frames_received/duration_s/fps_mean) below. Studio's UI
        (renderPhone/renderSessions in app.js) and list_sessions() only
        understand the latter - reshaping once here, rather than teaching
        every reader both shapes, is what keeps a transferred session's
        card from throwing on a field name that isn't there (which used to
        take the WHOLE session list down with it, not just that card)."""
        raw = None
        try:
            with open(os.path.join(dest_root, "session_meta.json")) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            pass    # the app always writes one, but don't block the transfer on it
        raw = raw or {}
        frames = raw.get("frames_written")
        meta = {
            "id": session_id, "origin": "transferred", "reason": "record locally -> transfer",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "params": {},
            "frames_received": frames if frames is not None else file_count,
            "frames_reported_sent": frames,
            "complete": True,
            "frames_captured_by_phone": frames,
            "frames_skipped_by_camera": raw.get("frames_dropped"),
            "resolution": None,
            "duration_s": round(raw.get("total_time_s") or 0.0, 3),
            "fps_median": None,
            "fps_mean": round(raw.get("fps") or 0.0, 2),
            "frame_gaps": raw.get("frames_dropped", 0),
            "non_monotonic_timestamps": None,
            "bytes": total_bytes,
            "imu_samples": raw.get("imu_samples", 0),
            "gps_fixes": raw.get("gps_fixes", 0),
            "boot_to_wall_s": raw.get("started_at_wall_ms"),
            "phone": {},
        }
        with open(os.path.join(dest_root, "session_meta.json"), "w") as f:
            json.dump(meta, f)
        return meta

    def _watchdog(self):
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            with self._lock:
                w = self.active
                if w is None:
                    continue
                if self._conn is not None:
                    w.disconnected_at = None
                if not w.confirmed and now - w.created > START_CONFIRM_TIMEOUT_S:
                    self._event("phone never confirmed START for %s - discarding" % w.id)
                    self._finalize(w, "phone never confirmed START")
                elif w.disconnected_at and now - w.disconnected_at > RESUME_GRACE_S:
                    self._finalize(w, "phone disconnected and did not come back")
                elif (w.origin == "legacy" and w.last_frame_at
                      and now - w.last_frame_at > LEGACY_IDLE_FINALIZE_S):
                    self._finalize(w, "legacy stream went idle")

    # ------------------------------------------------------------ packets --

    def _on_frame(self, pkt):
        with self._lock:
            w = self.active
            if w is None:
                if not self.v2:
                    w = self._open(new_session_id(self.root), {}, "legacy")
                else:
                    self.orphan_frames += 1
        if w is not None:
            w.add_frame(pkt)
        self._set_preview(pkt.jpeg)

    def _on_imu(self, samples):
        w = self.active
        if w is not None:
            w.add_imu(samples)

    def _on_gps(self, pkt):
        w = self.active
        if w is not None:
            w.add_gps(pkt)

    def _on_intrinsics(self, pkt):
        d = pkt._asdict()
        with self._lock:
            self.intrinsics = d
            if self.active is not None:
                self.active.intrinsics = d

    def _on_status(self, st):
        now = time.monotonic()
        with self._lock:
            self.status = st
            self.status_at = now
            if not self.v2:
                self.v2 = True
                self._event("remote control available (%s, app %s)"
                            % (st.get("model", "?"), st.get("app", "?")))
            state, sid = st.get("state"), st.get("session")
            w = self.active
            if w is not None:
                w.note_status(st)

            if state in ("recording", "finishing") and sid:
                if w is None or w.id != sid:
                    if w is not None:
                        self._finalize(w, "phone switched to session %s" % sid)
                    # Started from the phone's own button, or a take that is
                    # resuming its upload after this server restarted.
                    w = self._open(sid, st.get("params") or {}, "phone")
                    w.note_status(st)
                if not w.confirmed:
                    w.confirmed = True
                    self._event("phone is recording %s" % sid)
                if (state == "recording" and w.stop_requested_at is not None
                        and (w.stop_sent_at is None or now - w.stop_sent_at > STOP_RESEND_S)):
                    # STOP was pressed while the phone was unreachable, or the
                    # command was lost with a dying socket - ask again.
                    self._send_command({"cmd": "stop", "session": w.id})
                    w.stop_sent_at = now

            if w is not None and st.get("rejected") == w.id:
                self._event("phone refused START: %s" % st.get("error", "no reason given"))
                self._finalize(w, "phone refused START: %s" % st.get("error", ""))
            elif w is not None and state == "armed" and st.get("last_completed") == w.id:
                self._finalize(w, "complete", frames_sent=st.get("last_frames_sent"))

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

    # ---------------------------------------------------------- sessions --

    def _open(self, sid, params, origin):
        w = SessionWriter(self.root, sid, params, origin)
        w.intrinsics = self.intrinsics
        self.active = w
        self._event("session %s opened (%s)" % (sid, origin))
        return w

    def _finalize(self, w, reason, frames_sent=None):
        if self.active is w:
            self.active = None
        meta = w.finalize(reason, frames_sent)
        if meta is None:
            # Nothing was captured (refused/unconfirmed START): the directory
            # holds only an empty frames/ folder this class created moments
            # ago, so remove it rather than leave a husk in the session list.
            shutil.rmtree(w.dir, ignore_errors=True)
            self._event("session %s ended with no frames (%s)" % (w.id, reason))
            return
        self.last_finalized = meta
        missing = ""
        if frames_sent is not None and frames_sent != meta["frames_received"]:
            missing = " - WARNING: phone sent %d, received %d" % (frames_sent, meta["frames_received"])
        self._event("session %s saved: %d frames, %.1f s, %.1f fps (%s)%s"
                    % (w.id, meta["frames_received"], meta["duration_s"],
                       meta["fps_mean"], reason, missing))
        if self.on_session_finalized is not None:
            threading.Thread(target=self.on_session_finalized, args=(w.dir, meta),
                             daemon=True).start()

    # ---------------------------------------------------------- commands --

    def _send_command(self, obj):
        with self._send_lock:
            conn = self._conn
            if conn is None:
                raise ConnectionError("phone not connected")
            conn.sendall(protocol.encode_command(obj))

    def start_recording(self, params):
        """Returns (ok, session_id_or_error)."""
        with self._lock:
            if self._conn is None:
                return False, "phone not connected"
            if not self.v2:
                return False, ("the connected app does not support remote control - "
                               "install the updated rtvioapk build")
            if self.active is not None:
                return False, "already recording %s" % self.active.id
            if self.status.get("state") != "armed":
                return False, "phone is not ready (state: %s)" % self.status.get("state")
            sid = new_session_id(self.root)
            w = self._open(sid, params, "studio")
            try:
                self._send_command(dict(params, cmd="start", session=sid))
            except OSError as e:
                self._finalize(w, "could not send START: %s" % e)
                return False, "could not send START: %s" % e
            return True, sid

    def stop_recording(self):
        with self._lock:
            w = self.active
            if w is None:
                return False, "not recording"
            w.stop_requested_at = time.monotonic()
            if w.origin == "legacy":
                return False, "a legacy (v1) app can only be stopped from the phone"
            try:
                self._send_command({"cmd": "stop", "session": w.id})
                w.stop_sent_at = time.monotonic()
            except OSError:
                self._event("phone unreachable - STOP will be re-sent when it reconnects")
            return True, w.id

    def ping(self):
        try:
            self._send_command({"cmd": "ping"})
        except OSError:
            pass

    # ------------------------------------------------------------ readout --

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            st = dict(self.status)
            return {
                "connected": self._conn is not None,
                "peer": self.peer,
                "listen": "%s:%d" % (self.host, self.port),
                "listen_error": self.listen_error,
                "connected_s": round(now - self.connected_since, 1) if self.connected_since else None,
                "remote_control": self.v2,
                "status": st,
                "status_age_s": round(now - self.status_at, 1) if self.status_at else None,
                "recording": self.active.summary() if self.active is not None else None,
                "last_finalized": self.last_finalized,
                "orphan_frames": self.orphan_frames,
                "events": list(self.events)[-40:],
            }
