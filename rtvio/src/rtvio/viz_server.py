"""
Local live viewer for the reconstruction.

A tiny HTTP + Server-Sent-Events server that streams point-cloud batches,
the current pose, a throttled video preview, raw IMU samples and a GPS-fix
heartbeat to a browser tab as the live pipeline runs, so you can watch the
cloud grow and see what the phone is actually sending instead of only
reading the console counters. The GPS counter turns red after 10s of zero
fixes - with no GPS ever received, the pose has nothing external to
re-anchor to at all (see live_pipeline.py's module docstring "POSE COMES
FROM VISION, NOT IMU"): it runs on vision (solvePnPRansac) alone, which can
still drift in position and scale over a long session since the map PnP
matches against was itself built without any external reference.

THIS IS A PREVIEW CHANNEL, NOT A DATA PATH. It only observes points after
live_pipeline.py has already added them to the cloud (see
LiveReconstructor._reconstruct_keyframe) - it never reads from or writes to
the model. Closing the browser tab, or never opening one, changes nothing
about the reconstruction, and a slow/absent browser cannot back-pressure the
pipeline: pushes are best-effort and drop rather than block (see _Client).

Usage:
    python -m rtvio.live_pipeline --live-viz [--viz-port 8766]
    # then open http://localhost:8766 in a browser

Points are re-centred on the first point/pose seen (browsers lose float
precision on raw UTM-scale coordinates), and each keyframe's cloud is
decimated before broadcast (MAX_POINTS_PER_PUSH) - this is a preview, not
the deliverable asset, which is still written at full resolution by
_finalize() regardless of whether a viewer was ever attached.

A viewer that connects after the stream started only sees points broadcast
from that moment on; there is no history replay. Reopen alongside the
pipeline for the full session.
"""
import base64
import http.server
import json
import queue
import socketserver
import threading

import numpy as np

MAX_POINTS_PER_PUSH = 2500

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>RTVIO live cloud</title>
<style>
  html,body{margin:0;height:100%;background:#0b0d10;overflow:hidden;font:12px/1.4 -apple-system,Segoe UI,sans-serif;color:#cfd8e3}
  #hud{position:fixed;top:10px;left:12px;padding:8px 12px;background:rgba(10,12,16,.6);
       border:1px solid #2a3038;border-radius:8px;pointer-events:none;z-index:2}
  #hud b{color:#7fd3ff}
  #hud .warn{color:#ff8a65}
  #hud .ok{color:#4ed07a}
  #dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#e05a4e;margin-right:6px}
  #dot.live{background:#4ed07a}
  canvas{display:block}
  #cam{position:fixed;top:10px;right:10px;width:320px;border:1px solid #2a3038;
       border-radius:8px;background:#000;z-index:2;box-shadow:0 4px 18px rgba(0,0,0,.5)}
  #imu{position:fixed;bottom:10px;left:12px;padding:8px 12px;background:rgba(10,12,16,.6);
       border:1px solid #2a3038;border-radius:8px;pointer-events:none;z-index:2;
       font-variant-numeric:tabular-nums}
  #imu table{border-collapse:collapse}
  #imu td{padding:0 8px 0 0}
  #imuchart{display:block;margin-top:4px}
</style></head>
<body>
<div id="hud"><span id="dot"></span><span id="status">connecting...</span><br>
points <b id="npts">0</b> &nbsp; keyframes <b id="nkf">0</b> &nbsp;
gps fixes <b id="ngps">0</b> <span id="gpswarn"></span></div>
<img id="cam" alt="waiting for video...">
<div id="imu">
  <table>
    <tr><td>accel (m/s&sup2;)</td><td id="accel">-, -, -</td><td>|a|</td><td id="amag">-</td></tr>
    <tr><td>gyro (rad/s)</td><td id="gyro">-, -, -</td><td>|g|</td><td id="gmag">-</td></tr>
  </table>
  <canvas id="imuchart" width="320" height="60"></canvas>
</div>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0d10);
const camera = new THREE.PerspectiveCamera(60, innerWidth/innerHeight, 0.05, 20000);
camera.position.set(20, 20, 20);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);
const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.target.set(0,0,0);

const grid = new THREE.GridHelper(400, 40, 0x2a3038, 0x1a1e24);
scene.add(grid);
scene.add(new THREE.AxesHelper(5));

// ---- growable point cloud buffer ----
let capacity = 20000, count = 0;
let posArr = new Float32Array(capacity*3), colArr = new Float32Array(capacity*3);
let geom = new THREE.BufferGeometry();
function rebuildGeom(){
  geom.setAttribute('position', new THREE.BufferAttribute(posArr, 3));
  geom.setAttribute('color', new THREE.BufferAttribute(colArr, 3));
  geom.setDrawRange(0, count);
}
rebuildGeom();
const points = new THREE.Points(geom, new THREE.PointsMaterial({size:0.12, vertexColors:true}));
scene.add(points);

function ensureCapacity(extra){
  if (count + extra <= capacity) return;
  while (count + extra > capacity) capacity *= 2;
  const p2 = new Float32Array(capacity*3), c2 = new Float32Array(capacity*3);
  p2.set(posArr); c2.set(colArr);
  posArr = p2; colArr = c2;
  rebuildGeom();
}
function addPoints(rows){
  ensureCapacity(rows.length);
  for (const [x,y,z,r,g,b] of rows){
    const i = count*3;
    posArr[i]=x; posArr[i+1]=y; posArr[i+2]=z;
    colArr[i]=r/255; colArr[i+1]=g/255; colArr[i+2]=b/255;
    count++;
  }
  geom.attributes.position.needsUpdate = true;
  geom.attributes.color.needsUpdate = true;
  geom.setDrawRange(0, count);
  document.getElementById('npts').textContent = count.toLocaleString();
}

// ---- trajectory ----
const trajPts = [];
const trajGeom = new THREE.BufferGeometry();
const trajLine = new THREE.Line(trajGeom, new THREE.LineBasicMaterial({color:0x7fd3ff}));
scene.add(trajLine);
const marker = new THREE.Mesh(new THREE.SphereGeometry(0.25, 12, 12),
                               new THREE.MeshBasicMaterial({color:0xffcc55}));
scene.add(marker);
let nkf = 0, followFrames = 0;
function addPose(p){
  trajPts.push(new THREE.Vector3(p[0], p[1], p[2]));
  trajGeom.setFromPoints(trajPts);
  marker.position.set(p[0], p[1], p[2]);
  if (followFrames++ % 30 === 0) controls.target.lerp(marker.position, 0.5);
}

// ---- video preview ----
const camImg = document.getElementById('cam');
function showFrame(b64){ camImg.src = 'data:image/jpeg;base64,' + b64; }

// ---- IMU readout + rolling strip chart ----
const accelEl = document.getElementById('accel'), gyroEl = document.getElementById('gyro');
const amagEl = document.getElementById('amag'), gmagEl = document.getElementById('gmag');
const ichart = document.getElementById('imuchart'), ictx = ichart.getContext('2d');
const AMAX = 25, GMAX = 6;             // rough full-scale for the strip chart, m/s^2 and rad/s
let ihist = [];                        // recent {a, g} magnitudes
function showImu(d){
  const [ax, ay, az, gx, gy, gz] = d;
  const amag = Math.hypot(ax, ay, az), gmag = Math.hypot(gx, gy, gz);
  accelEl.textContent = ax.toFixed(2)+', '+ay.toFixed(2)+', '+az.toFixed(2);
  gyroEl.textContent = gx.toFixed(2)+', '+gy.toFixed(2)+', '+gz.toFixed(2);
  amagEl.textContent = amag.toFixed(2);
  gmagEl.textContent = gmag.toFixed(2);
  ihist.push({a: amag, g: gmag});
  if (ihist.length > 160) ihist.shift();
  ictx.clearRect(0, 0, ichart.width, ichart.height);
  ictx.strokeStyle = '#7fd3ff'; ictx.beginPath();
  ihist.forEach((s, i) => {
    const x = i / 160 * ichart.width, y = ichart.height - Math.min(s.a / AMAX, 1) * ichart.height;
    i === 0 ? ictx.moveTo(x, y) : ictx.lineTo(x, y);
  });
  ictx.stroke();
  ictx.strokeStyle = '#ffcc55'; ictx.beginPath();
  ihist.forEach((s, i) => {
    const x = i / 160 * ichart.width, y = ichart.height - Math.min(s.g / GMAX, 1) * ichart.height;
    i === 0 ? ictx.moveTo(x, y) : ictx.lineTo(x, y);
  });
  ictx.stroke();
}

// ---- GPS fix counter ----
let ngps = 0;
const ngpsEl = document.getElementById('ngps'), gpswarnEl = document.getElementById('gpswarn');
let firstMsgAt = null;
function noteGps(){
  ngps++; ngpsEl.textContent = ngps; ngpsEl.className = 'ok';
  gpswarnEl.textContent = '';
}

const dot = document.getElementById('dot'), status = document.getElementById('status');
const es = new EventSource('/events');
es.onopen = () => { dot.classList.add('live'); status.textContent = 'live'; };
es.onerror = () => { dot.classList.remove('live'); status.textContent = 'reconnecting...'; };
es.onmessage = (ev) => {
  if (firstMsgAt === null) firstMsgAt = performance.now();
  const msg = JSON.parse(ev.data);
  if (msg.t === 'pts'){ addPoints(msg.d); nkf++; document.getElementById('nkf').textContent = nkf; }
  else if (msg.t === 'pose'){ addPose(msg.d); }
  else if (msg.t === 'frame'){ showFrame(msg.d); }
  else if (msg.t === 'imu'){ showImu(msg.d); }
  else if (msg.t === 'gps'){ noteGps(); }
};
// If 10s of live data has gone by with zero GPS fixes, the EKF is running on
// pure IMU dead-reckoning - flag it, since that is the single most common
// cause of a trajectory that drifts steadily in one direction (see
// live_pipeline.py's COURSE_TIMEOUT_S / update_gps).
setInterval(() => {
  if (firstMsgAt !== null && ngps === 0 && performance.now() - firstMsgAt > 10000) {
    ngpsEl.className = 'warn';
    gpswarnEl.textContent = '(no GPS - position is IMU dead-reckoning only, will drift)';
  }
}, 2000);

window.addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
(function animate(){
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
})();
</script>
</body></html>
"""


class _Client:
    """One connected browser tab's outbound queue.

    A slow or stalled tab must never back-pressure the reconstruction
    threads that push into it, so sends are non-blocking and drop the
    oldest queued message rather than block - the newest point batch
    matters more to "is it still growing" than one that's now stale.
    """

    def __init__(self):
        self.q = queue.Queue(maxsize=200)

    def send(self, msg):
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(msg)
            except queue.Full:
                pass


class LiveViz:
    """Owns the HTTP/SSE server and the set of connected browser tabs."""

    def __init__(self, port=8766):
        self.port = port
        self._clients = []
        self._lock = threading.Lock()
        self._origin = None
        self._httpd = None
        self._thread = None

    def start(self):
        viz = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # Deliberately NOT HTTP/1.1: /events has no Content-Length and is
            # never chunked, so under 1.1's framing rules a spec-compliant
            # client (including Python's own http.client) treats a
            # keep-alive response with neither as a ZERO-LENGTH body and
            # never delivers a byte of it. HTTP/1.0's rule is simpler and is
            # what we want here: no Content-Length means "read until the
            # connection closes", which for a single long-lived GET per
            # browser tab is exactly right - no chunked framing needed.
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass  # keep the pipeline's own console output clean

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    body = PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    client = _Client()
                    with viz._lock:
                        viz._clients.append(client)
                    try:
                        while True:
                            msg = client.q.get()
                            self.wfile.write(("data: %s\n\n" % msg).encode("utf-8"))
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with viz._lock:
                            if client in viz._clients:
                                viz._clients.remove(client)
                    return
                self.send_response(404)
                self.end_headers()

        self._httpd = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _broadcast(self, msg):
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.send(msg)

    def push_points(self, pts, cols):
        """Called from a dense-lane worker thread once a keyframe's points
        exist. Decimates before sending - the preview does not need every
        point, the export at session end still gets all of them."""
        n = len(pts)
        if n == 0:
            return
        if self._origin is None:
            self._origin = np.asarray(pts[0], dtype=np.float64)
        if n > MAX_POINTS_PER_PUSH:
            idx = np.linspace(0, n - 1, MAX_POINTS_PER_PUSH).astype(np.int64)
            pts, cols = np.asarray(pts)[idx], np.asarray(cols)[idx]
        rel = np.asarray(pts, dtype=np.float64) - self._origin
        rows = np.concatenate([rel, np.clip(cols, 0, 255)], axis=1)
        self._broadcast(json.dumps({"t": "pts", "d": rows.round(3).tolist()}))

    def push_pose(self, p):
        """Called from the reader thread on (roughly) every frame."""
        if self._origin is None:
            self._origin = np.asarray(p, dtype=np.float64)
        rel = np.asarray(p, dtype=np.float64) - self._origin
        self._broadcast(json.dumps({"t": "pose", "d": [round(float(v), 3) for v in rel]}))

    def push_frame(self, jpeg_bytes):
        """Called from the reader thread with the phone's own JPEG bytes -
        no re-encoding, just base64 for embedding in the SSE text stream.
        Caller is expected to throttle (see live_pipeline.py); this method
        does not decimate on its own."""
        self._broadcast(json.dumps({
            "t": "frame", "d": base64.b64encode(jpeg_bytes).decode("ascii")}))

    def push_imu(self, accel, gyro):
        """Called from the reader thread on (a throttled subset of) IMU
        samples. accel/gyro are body-frame triples straight off the wire -
        this is the raw sensor feed, not anything the EKF has touched."""
        d = [round(float(v), 4) for v in accel] + [round(float(v), 4) for v in gyro]
        self._broadcast(json.dumps({"t": "imu", "d": d}))

    def push_gps(self):
        """Called from the reader thread on every fused GPS fix. The browser
        only needs a heartbeat to answer "is GPS arriving at all", so no
        payload beyond the message type."""
        self._broadcast(json.dumps({"t": "gps"}))

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
