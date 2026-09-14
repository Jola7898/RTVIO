"""
Static load-and-view for a finished reconstruction (mesh_poisson.glb) -
the "Web-based ... Viewer" deliverable SIH26158 asks for, and the 5%-
weighted UI criterion. Session 3's plan item 5 - see
~/.claude/plans/okay-tell-me-what-ancient-puddle.md.

Deliberately NOT a repurpose of viz_server.py: that module is built
entirely around a live SSE push from a running reconstruction
(LiveReconstructor._reconstruct_keyframe) - there is nothing live to push
here, just a finished .glb sitting in an output directory. A plain static
file server plus a three.js viewer page is simpler and has nothing to get
wrong about a stream that doesn't exist. Same three.js version (r128) and
global-script (non-module) convention as viz_server.py, for consistency -
not because r128 is special.

Usage:
    python -m rtvio.view_output <out_dir> [--port 8080] [--mesh mesh_poisson.glb]
    # then open http://localhost:8080 in a browser (or pass --open to do
    # that automatically)
"""
import argparse
import http.server
import os
import threading
import webbrowser

PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>RTVIO reconstruction viewer</title>
<style>
  html,body{margin:0;height:100%;background:#0b0d10;overflow:hidden;font:12px/1.4 -apple-system,Segoe UI,sans-serif;color:#cfd8e3}
  #hud{position:fixed;top:10px;left:12px;padding:8px 12px;background:rgba(10,12,16,.6);
       border:1px solid #2a3038;border-radius:8px;pointer-events:none;z-index:2}
  #hud b{color:#7fd3ff}
  #hud .err{color:#ff8a65}
  canvas{display:block}
</style></head>
<body>
<div id="hud"><b>RTVIO</b> - __MESH_NAME__<br><span id="status">loading...</span></div>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js"></script>
<script>
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0d10);
const camera = new THREE.PerspectiveCamera(60, innerWidth/innerHeight, 0.01, 1e6);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
document.body.appendChild(renderer.domElement);

const controls = new THREE.OrbitControls(camera, renderer.domElement);

// Reconstructions can be real-world UTM-ish ENU (tens/hundreds of metres)
// or VGGT's own small unanchored units (relative mode, ~1-2 units wide -
// see vggt_reconstruct.py's RELATIVE MODE comments) - no fixed light/grid/
// camera distance works for both, so everything below is scaled from the
// loaded mesh's own bounding box once it's known, not hardcoded.
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 0.8);
sun.position.set(1, 2, 1);
scene.add(sun);

const statusEl = document.getElementById('status');
const loader = new THREE.GLTFLoader();
loader.load('__MESH_PATH__', (gltf) => {
  const mesh = gltf.scene;
  scene.add(mesh);

  const box = new THREE.Box3().setFromObject(mesh);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 1e-6);

  scene.add(new THREE.GridHelper(radius * 3, 20, 0x2a3038, 0x1a1e24));
  scene.add(new THREE.AxesHelper(radius * 0.2));

  camera.position.set(center.x + radius, center.y + radius, center.z + radius);
  camera.near = radius / 1000;
  camera.far = radius * 100;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();

  statusEl.textContent = 'loaded (' + size.x.toFixed(1) + ' x ' + size.y.toFixed(1)
                          + ' x ' + size.z.toFixed(1) + ' units)';
}, undefined, (err) => {
  statusEl.innerHTML = '<span class="err">failed to load __MESH_NAME__ - ' +
                        'is it actually in this output directory?</span>';
  console.error(err);
});

window.addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

(function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
})();
</script>
</body></html>
"""


def _make_handler(out_dir, mesh_name):
    # .replace(), not %-formatting: the template is full of literal CSS/JS
    # '%' (e.g. "height:100%") that %-style substitution would misparse as
    # format specifiers - confirmed by hitting exactly that ValueError
    # ("unsupported format character ';'") when this used PAGE_TEMPLATE % {...}.
    page = (PAGE_TEMPLATE.replace("__MESH_NAME__", mesh_name)
                          .replace("__MESH_PATH__", mesh_name)).encode("utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=out_dir, **kwargs)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            super().do_GET()

        def log_message(self, fmt, *args):
            pass  # SimpleHTTPRequestHandler's default is noisy per-request stderr spam

    # .glb has no default entry in Python's mimetypes module on some
    # platforms - without this it's served as application/octet-stream,
    # which GLTFLoader's XHR fetch tolerates fine, but declaring it
    # correctly costs nothing and is one less thing to wonder about if a
    # load ever fails.
    Handler.extensions_map = dict(Handler.extensions_map)
    Handler.extensions_map[".glb"] = "model/gltf-binary"
    Handler.extensions_map[".gltf"] = "model/gltf+json"
    return Handler


def serve(out_dir, port=8080, mesh_name="mesh_poisson.glb", open_browser=False):
    mesh_path = os.path.join(out_dir, mesh_name)
    if not os.path.exists(mesh_path):
        print("WARNING: %s not found in %s - the page will load but the "
              "viewer will show a load error. Pass --mesh to point at a "
              "different file (mesh_2p5d.obj isn't supported by this "
              "viewer yet - glTF/.glb only)." % (mesh_name, out_dir))

    handler = _make_handler(out_dir, mesh_name)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = "http://127.0.0.1:%d" % port
    print("serving %s at %s (Ctrl+C to stop)" % (out_dir, url))
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", help="a vggt_reconstruct.py output directory")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mesh", default="mesh_poisson.glb",
                     help="glTF/.glb file to load, relative to out_dir")
    ap.add_argument("--open", action="store_true", help="open a browser tab automatically")
    args = ap.parse_args()
    serve(args.out_dir, port=args.port, mesh_name=args.mesh, open_browser=args.open)


if __name__ == "__main__":
    main()
