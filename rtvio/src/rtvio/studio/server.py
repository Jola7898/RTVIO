"""
RTVIO Studio - browser control for phone capture + VGGT reconstruction.

    python -m rtvio.studio [--port 8080] [--phone-port 5555] [--open]

Then, on the phone: Settings -> Server IP = this PC's LAN address, port
5555 -> back -> CONNECT. The page at http://127.0.0.1:8080 shows the
phone's viewfinder, starts/stops recordings on it, queues every finished
take for reconstruction, and opens cloud_raw.ply / mesh_poisson.ply in a
3D viewer.

The web UI binds to 127.0.0.1 unless --web-host says otherwise: it can start
the phone's camera and run GPU jobs, and has no authentication, so exposing
it to the LAN is an explicit choice. The phone port has to be on the LAN.
"""
import argparse
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .jobs import GpuMonitor, ReconQueue
from .phone_link import PhoneLink

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PACKAGE_DIR, "web")
RTVIO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PACKAGE_DIR)))
DEFAULT_DATA_ROOT = os.path.join(RTVIO_ROOT, "data")

# Capture defaults are chosen for "use every frame": 720p rather than 1080p
# because VGGT resizes every frame to 518 px on its long side anyway, so 1080p
# only costs phone encode time and WiFi bandwidth (the two things that cost
# frames) without adding anything the model can see. JPEG 90 because at 518 px
# the model does see blocking artifacts from low qualities.
DEFAULT_SETTINGS = {
    "capture": {"resolution": "720p", "fps": 30, "jpeg_quality": 90,
                "gps": False, "imu": True},
    # Indoor test defaults: no GPS (unreliable indoors), vision-only
    # alignment, every frame used.
    "recon": {"window_frames": "auto", "overlap": 8, "frame_stride": 1,
              "conf_percentile": 50, "poisson_depth": 10, "voxel_factor": 1.0,
              "min_views": 2, "gps_mode": "off", "masking": False, "extras": False},
    "auto_reconstruct": True,
}

SESSION_ID_RE = re.compile(r"^[\w.-]+$")
PRIORITY_OUTPUTS = ("cloud_raw.ply", "mesh_poisson.ply")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".jpg": "image/jpeg", ".png": "image/png", ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".ply": "application/octet-stream", ".obj": "text/plain; charset=utf-8",
    ".glb": "model/gltf-binary", ".las": "application/octet-stream",
}


def lan_addresses():
    """Best-effort list of this PC's LAN IPv4 addresses, for telling the user
    what to type into the phone. The UDP-connect trick finds the address of
    the default route's interface without sending anything."""
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Studio:
    def __init__(self, data_root, phone_host, phone_port):
        self.data_root = data_root
        self.sessions_root = os.path.join(data_root, "sessions")
        os.makedirs(self.sessions_root, exist_ok=True)
        self.settings_path = os.path.join(data_root, "studio_settings.json")
        self.settings = _merge(DEFAULT_SETTINGS, self._load_settings())
        self.gpu = GpuMonitor().start()
        self.queue = ReconQueue(self.gpu, cwd=RTVIO_ROOT).start()
        self.phone = PhoneLink(self.sessions_root, phone_host, phone_port,
                               on_session_finalized=self._on_finalized)
        self.phone.start()
        self.lan = lan_addresses()
        self.phone_port = phone_port

    # ---------------------------------------------------------- settings --

    def _load_settings(self):
        try:
            with open(self.settings_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def update_settings(self, patch):
        self.settings = _merge(self.settings, patch)
        with open(self.settings_path, "w") as f:
            json.dump(self.settings, f, indent=2)
        return self.settings

    # ---------------------------------------------------------- sessions --

    def _on_finalized(self, session_dir, meta):
        if self.settings.get("auto_reconstruct", True):
            self.queue.submit(session_dir, self.settings["recon"])

    def session_dir(self, sid):
        if not SESSION_ID_RE.match(sid or ""):
            return None
        d = os.path.join(self.sessions_root, sid)
        return d if os.path.isdir(d) else None

    def list_sessions(self, limit=50):
        active = self.phone.active.id if self.phone.active is not None else None
        out = []
        try:
            names = sorted(os.listdir(self.sessions_root), reverse=True)
        except OSError:
            names = []
        for sid in names[:limit]:
            d = os.path.join(self.sessions_root, sid)
            if not os.path.isdir(d):
                continue
            meta = None
            try:
                with open(os.path.join(d, "session_meta.json")) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            recons = []
            for name in sorted((n for n in os.listdir(d) if n.startswith("recon-")),
                               key=lambda n: int(n.split("-")[1]) if n.split("-")[1].isdigit() else 0):
                rd = os.path.join(d, name)
                prog = None
                try:
                    with open(os.path.join(rd, "progress.json")) as f:
                        prog = json.load(f)
                except (OSError, ValueError):
                    pass
                files = {}
                for fn in PRIORITY_OUTPUTS + ("CHECKPOINT_REPORT.md", "trajectory_check.png",
                                              "cameras.json", "job.log"):
                    p = os.path.join(rd, fn)
                    if os.path.exists(p):
                        files[fn] = os.path.getsize(p)
                recons.append({"name": name, "progress": prog, "files": files})
            out.append({
                "id": sid,
                "recording": sid == active,
                "meta": meta,
                "thumb": "frames/000000.jpg" if os.path.exists(os.path.join(d, "frames", "000000.jpg")) else None,
                "recons": recons,
                "busy": self.queue.busy_with(sid),
            })
        return out

    def state(self):
        return {
            "time": time.time(),
            "phone": self.phone.snapshot(),
            "jobs": self.queue.snapshot(),
            "gpu": self.gpu.snapshot(n=90),
            "settings": self.settings,
            "lan": self.lan,
            "phone_port": self.phone_port,
        }


def make_handler(studio):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------ helpers --

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                obj = json.loads(self.rfile.read(n).decode("utf-8"))
                return obj if isinstance(obj, dict) else {}
            except ValueError:
                return {}

        def _file(self, path):
            try:
                size = os.path.getsize(path)
            except OSError:
                return self._send(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(
                os.path.splitext(path)[1].lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        @staticmethod
        def _inside(root, rel):
            p = os.path.realpath(os.path.join(root, rel))
            r = os.path.realpath(root)
            return p if p == r or p.startswith(r + os.sep) else None

        # --------------------------------------------------------- GET --

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(url.path)
            if path in ("/", "/index.html"):
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/static/"):
                p = self._inside(WEB_DIR, path[len("/static/"):])
                return self._file(p) if p else self._send(404, {"error": "not found"})
            if path == "/api/state":
                return self._send(200, studio.state())
            if path == "/api/sessions":
                return self._send(200, studio.list_sessions())
            if path == "/api/preview.jpg":
                _seq, jpeg = studio.phone.latest_seq, studio.phone.latest_jpeg
                if jpeg is None:
                    return self._send(404, {"error": "no frame yet"})
                return self._send(200, jpeg, "image/jpeg")
            if path == "/api/preview.mjpg":
                return self._mjpeg()
            m = re.match(r"^/api/jobs/(\d+)/log$", path)
            if m:
                job = next((j for j in studio.queue.jobs if j.id == int(m.group(1))), None)
                if job is None:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, {"lines": job.log_tail(200)})
            m = re.match(r"^/files/([\w.-]+)/(.+)$", path)
            if m:
                d = studio.session_dir(m.group(1))
                p = self._inside(d, m.group(2)) if d else None
                return self._file(p) if p else self._send(404, {"error": "not found"})
            return self._send(404, {"error": "not found"})

        def _mjpeg(self):
            """multipart/x-mixed-replace: an <img> tag renders it as live
            video with no JavaScript. Capped at ~12 fps - it is a viewfinder,
            and localhost bandwidth is not the concern, the browser's JPEG
            decode is."""
            boundary = "rtvioframe"
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=%s" % boundary)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            last = 0.0
            try:
                while True:
                    seq, jpeg = studio.phone.wait_preview(seq, timeout=2.0)
                    if jpeg is None:
                        continue
                    wait = 1.0 / 12 - (time.monotonic() - last)
                    if wait > 0:
                        time.sleep(wait)
                    last = time.monotonic()
                    self.wfile.write(("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                      % (boundary, len(jpeg))).encode("ascii"))
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (ConnectionError, OSError):
                return

        # -------------------------------------------------------- POST --

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self._json_body()
            if path == "/api/record/start":
                params = _merge(studio.settings["capture"], body.get("capture") or {})
                ok, res = studio.phone.start_recording(params)
                return self._send(200 if ok else 409, {"ok": ok, "session": res} if ok else {"ok": False, "error": res})
            if path == "/api/record/stop":
                ok, res = studio.phone.stop_recording()
                return self._send(200 if ok else 409, {"ok": ok, "session": res} if ok else {"ok": False, "error": res})
            if path == "/api/phone/ping":
                studio.phone.ping()
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                return self._send(200, studio.update_settings(body))
            m = re.match(r"^/api/sessions/([\w.-]+)/reconstruct$", path)
            if m:
                d = studio.session_dir(m.group(1))
                if d is None:
                    return self._send(404, {"ok": False, "error": "no such session"})
                if not os.path.exists(os.path.join(d, "frame_timestamps.json")):
                    return self._send(409, {"ok": False, "error": "session is still recording"})
                job = studio.queue.submit(d, _merge(studio.settings["recon"], body.get("recon") or {}))
                return self._send(200, {"ok": True, "job": job.id})
            m = re.match(r"^/api/jobs/(\d+)/cancel$", path)
            if m:
                return self._send(200, {"ok": studio.queue.cancel(int(m.group(1)))})
            return self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--web-host", default="127.0.0.1",
                    help="interface for the web UI (default: this PC only; 0.0.0.0 exposes "
                         "the unauthenticated control page to the whole LAN)")
    ap.add_argument("--port", type=int, default=8080, help="web UI port")
    ap.add_argument("--phone-host", default="0.0.0.0")
    ap.add_argument("--phone-port", type=int, default=5555, help="port the app connects to")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="sessions are written to <data-root>/sessions/<id>/")
    ap.add_argument("--open", action="store_true", help="open the page in a browser")
    args = ap.parse_args()

    studio = Studio(args.data_root, args.phone_host, args.phone_port)
    httpd = ThreadingHTTPServer((args.web_host, args.port), make_handler(studio))
    httpd.daemon_threads = True
    url = "http://%s:%d" % ("127.0.0.1" if args.web_host in ("0.0.0.0", "") else args.web_host, args.port)
    print("RTVIO Studio: %s" % url)
    print("phone: set Server IP to one of %s, port %d" % (", ".join(studio.lan) or "<this PC's LAN IP>",
                                                          args.phone_port))
    print("sessions: %s" % studio.sessions_root, flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
