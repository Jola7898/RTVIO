# iDronam integration notes

Imported from the `idronam` branch (`session.md`, commit cdd2696) when its
`capture-bridge/` was folded into RTVIO Studio. The Node bridge itself
(Express + Socket.IO + a vendored `node.exe`/`ffmpeg.exe`) is not carried
over: its connection logic now lives in `src/rtvio/studio/drone_link.py`
(MAVLink over TCP, RTSP video, recording) and `src/rtvio/studio/mavlink.py`
(the codec, with message layouts taken from the bridge's `lib/mavlink20.js`).
The original is still on that branch. Below is the original write-up,
unchanged - "capture-bridge" and "config.json" refer to that Node tool.

---
# Session Notes — iDronam Telemetry/Video Tap

**Goal:** Suparna 5G drone + Menthosa's iDronam GCS. Figure out how iDronam
gets telemetry and camera feed from the drone, then build our own local
capture path for the same streams — eventually to feed an RTVIO 3D
reconstruction pipeline instead of (or alongside) iDronam.

## 1. How we investigated it

You gave me the `Local` and `Roaming` AppData folders for `iDronam-Enterprise`.
That turned out to be more than a Chromium profile — the actual installed
app (Electron) lived right there too: `Local\iDronam-Enterprise\iDronam-Enterprise.exe`
and `resources\app.asar`.

`app.asar` is just a JSON directory header + concatenated file contents
(Chromium's `Pickle` format) — no special tool needed. I wrote a ~40-line
Python script to unpack it and got the app's actual source: an Electron
main process plus four bundled Node/Express backends
(`prod.serv/{m,ma,v,r}.p.js`).

From there it was direct code reading, not guessing: which packages each
bundle `require()`s, which ports they `.listen()` on, which `net.connect()`
/`dgram.createSocket()` calls exist, and which Socket.IO events get
`.emit()`/`.on()`'d.

## 2. What iDronam actually does

The Electron window doesn't render a local file — it loads a 1-line
bootstrap that redirects to `http://localhost:8323`. **The entire GCS UI is
served by iDronam's own local backend**, not shipped as static assets.

Four local services, all spun up by the main process:

| Service | Port | Purpose |
|---|---|---|
| `m.p.js` | 8323 | Main service: serves the dashboard, holds the MAVLink link to the drone, Socket.IO push to the UI, mirrors telemetry to Menthosa's cloud (`telem.idronam.com`) |
| `ma.p.js` | 8324 | Local OSM map-tile cache (`/osm/:z/:x/:y.png`) — unrelated to the drone |
| `v.p.js` | 4550 | Video relay: browser opens `ws://localhost:4550/?token=<drone_ip>&...`, server spawns bundled `ffmpeg` to pull `rtsp://<drone_ip>:10000/drone_cam` and re-streams it as MPEG-TS over that WebSocket (decoded client-side with JSMpeg) |
| `r.p.js` | 8325 | RTK bridge: talks to a serial-connected RTK receiver, forwards `GPS_RTCM_DATA` corrections |

**Telemetry:** `m.p.js` opens a plain **TCP** socket
(`net.connect({host: device_ip, port: device_port})`, default port
**14550**) to the drone/companion computer — not UDP, despite the
misleadingly-named `video_port: 14550` field in the device object. Incoming
bytes go into a `MAVLink20Processor` (the standard, publicly-documented
MAVLink v2 JS bindings — this is generated code from MAVLink's own code
generator, not Menthosa IP; every MAVLink GCS ships an equivalent copy).
Right after connecting it sends a `REQUEST_DATA_STREAM` (all streams) and
then a 1 Hz GCS heartbeat, using MAVLink identity **system id 255,
component id 1**. Every decoded message is re-emitted as a Socket.IO event
named after the MAVLink message (`HEARTBEAT`, `ATTITUDE`,
`GLOBAL_POSITION_INT`, `GPS_RAW_INT`, `SYS_STATUS`, `BATTERY_STATUS`, ~33
in total).

**Camera/gimbal control:** a UDP socket connected to `device_ip:37260` —
the standard SIYI gimbal UDP control port.

**Cloud mirror:** the same telemetry is also pushed via `socket.io-client`
to `https://telem.idronam.com/vehicle` unless a dev-mode flag points it at
`127.0.0.1:3500` instead. Worth knowing if you care about the data staying
fully local/offline.

One incidental finding, not exploited, just noted: the bundle has a
hard-coded `express-session` secret string baked into shipped client code.

## 3. What we built: `capture-bridge/`

A standalone capture bridge, independent of iDronam, that talks to the
drone directly:

- **Connects the same way iDronam does** — TCP to `device_ip:14550`, MAVLink
  v2, GCS identity 255/1, `REQUEST_DATA_STREAM` + heartbeat on connect —
  so it works with the same drone-side setup with no drone/firmware config
  changes.
- **Reuses iDronam's own MAVLink parser.** Rather than hand-roll a MAVLink
  decoder, I extracted the exact generated `mavlink20`/`MAVLink20Processor`
  module (webpack module `5446` inside `m.p.js`) out of the bundle and
  wrapped it as a standalone file: `capture-bridge/lib/mavlink20.js`. It's
  the public MAVLink protocol's own generated bindings, so this guarantees
  identical decoding to iDronam, field for field.
- **Fully self-contained, no installs.** iDronam's install already carries
  a full Node 20 runtime (`resources/app.asar.unpacked/node_modules/node/bin/node.exe`)
  and every pure-JS dependency the parser needs (`jspack`, `underscore`,
  `long`) plus `express`/`socket.io`/`ws` inside its packed `app.asar`. I
  copied the runtime to `capture-bridge/vendor/node.exe` and resolved+copied
  the 87-package dependency closure into `capture-bridge/node_modules/` (no
  npm registry access needed or used).
- **Design choice — direct connection vs. tapping iDronam's Socket.IO:**
  iDronam only opens the drone TCP link when a Socket.IO client connects
  with a valid, pre-registered `device_alias` (looked up server-side), so
  "just subscribe to iDronam's existing broadcast" isn't actually free —
  we'd need a saved device profile's credentials either way. Connecting to
  the drone ourselves, independently, was equally easy and doesn't require
  iDronam to be running at all — which also matches the end goal of
  eventually not depending on iDronam.

Structure:

```
capture-bridge/
  vendor/node.exe     Node runtime (from the iDronam install)
  node_modules/        express, socket.io, mavlink parser deps (87 pkgs)
  lib/mavlink20.js      extracted MAVLink v2 parser (public protocol code)
  server.js             TCP connect -> decode -> Socket.IO + REST
  public/index.html     live dashboard (heartbeat/GPS/attitude/battery/sys)
  config.json            device_ip / device_port / http_port
  start.bat
  README.md              setup + usage + troubleshooting
```

Run: `start.bat`, then open `http://localhost:8080`. Full instructions are
in `capture-bridge/README.md`.

**Status as of this session:** `config.json` has been filled in with the
drone's IP (`172.16.0.160`, port 14550) — connection testing against the
live, powered-on drone was in progress.

## 4. Cleanup done this session

The raw AppData copies (`Local\iDronam-Enterprise`, `Roaming\iDronam-Enterprise`)
were only needed to reverse-engineer the app; `capture-bridge` no longer
depends on them (own Node runtime, own copied dependencies, own extracted
parser module). Removed the Chromium cache/profile bloat from both:

- Deleted from `Local\iDronam-Enterprise`: `Cache`, `Code Cache`,
  `blob_storage`, `DawnGraphiteCache`, `DawnWebGPUCache`, `GPUCache`,
  `Local Storage`, `Network`, `Session Storage`, `Shared Dictionary`,
  `shared_proto_db`, `VideoDecodeStats`, `DIPS`, `SharedStorage`.
- Deleted `Roaming\iDronam-Enterprise` entirely — it was 100% duplicate
  Chromium cache, no app files.
- **Kept** `iDronam-Enterprise.exe` and `resources\` (incl. `app.asar` /
  `app.asar.unpacked`) — the real, runnable app, in case we need to launch
  it again, re-check its source for the video piece, or pull the bundled
  `ffmpeg.exe` from `app.asar.unpacked`.
- Net: `Local` + `Roaming` went from ~991 MB to ~762 MB, all in the app
  binary/runtime, not cache.

## 5. Not built yet

- **Video capture.** iDronam pulls `rtsp://<drone_ip>:10000/drone_cam` via
  its bundled `ffmpeg` binary (also sitting in
  `Local\iDronam-Enterprise\resources\app.asar.unpacked\node_modules\ffmpeg-static\ffmpeg.exe`,
  reusable the same way the Node runtime was). Next step would be spawning
  that against the same RTSP URL and either re-serving it over WebSocket
  (jsmpeg-style, like iDronam does) or writing frames/timestamps straight
  to disk for RTVIO.
- **RTVIO integration itself** — this session only covers getting the raw
  telemetry + (soon) video streams flowing locally; wiring them into the
  actual 3D reconstruction pipeline is future work.
