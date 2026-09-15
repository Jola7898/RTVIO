# Drone Telemetry Tap (capture-bridge)

A standalone capture bridge that connects **directly to the Suparna drone**
over MAVLink v2 — independent of iDronam — and republishes decoded telemetry
to a local web dashboard and a JSON API, as a first step toward feeding your
own RTVIO pipeline.

It does not touch, modify, or interfere with iDronam. It's a second,
independent client speaking the same open MAVLink protocol iDronam uses.

## How it was built

Everything here (`lib/mavlink20.js`, the connection sequence in `server.js`)
was reconstructed from reading iDronam's own bundled code
(`resources/app.asar`), so it talks to your specific drone stack the same
way iDronam does:

- TCP connection to `device_ip:device_port` (default port **14550**)
- GCS identity: MAVLink system id 255, component id 1
- Sends a `REQUEST_DATA_STREAM` (all streams) right after connecting, plus
  a 1 Hz GCS heartbeat — same as iDronam
- `lib/mavlink20.js` is the standard MAVLink-generated JS parser (public
  protocol, not Menthosa IP) extracted verbatim from iDronam's bundle

Everything runs on the Node runtime and npm packages already bundled inside
your iDronam install (`vendor/node.exe`, `node_modules/`) — no internet
access or separate Node.js install needed.

## Setup (one-time)

1. Put `node.exe` and `ffmpeg.exe` in `vendor/` (see `vendor/README.md` for
   where to get them), and run `npm install` in this folder to pull in
   `express`/`socket.io`/the MAVLink parser's dependencies.
2. Copy `config.example.json` to `config.json` and set `"device_ip"` to your
   drone's IP address —
   the same value you'd type into iDronam's "Add Device" screen.
   - Easiest way to find it: open the real iDronam app and check the saved
     device, or connect your PC to the drone's network and check your
     adapter's gateway (`ipconfig`).
2. Leave `device_port` at `14550` unless iDronam's device profile uses a
   different port.

## Running it (drone powered on)

Double-click `start.bat`, or from a terminal:

```
vendor\node.exe server.js
```

Then open **http://localhost:8080** in a browser. You do NOT need iDronam
running at all — this connects to the drone on its own. (It's also safe to
run at the same time as iDronam; the drone-side MAVLink server generally
accepts more than one TCP client.)

You should see the connection dot turn green within a few seconds of a
successful TCP connect, and live HEARTBEAT/GPS/attitude/battery values
start updating.

### Command-line overrides

```
vendor\node.exe server.js --ip 192.168.144.20 --port 14550 --http 8080
```

### Consuming the data programmatically (for RTVIO later)

- Socket.IO: connect to `http://localhost:8080`, listen for events named
  after MAVLink messages (`HEARTBEAT`, `ATTITUDE`, `GLOBAL_POSITION_INT`,
  `GPS_RAW_INT`, `SYS_STATUS`, `BATTERY_STATUS`, ...), plus a raw
  `MAVLINK_PKT` event carrying the untouched bytes if you'd rather parse
  timing-sensitive messages yourself.
- REST snapshot: `GET http://localhost:8080/api/latest`

## Not built yet: video

iDronam pulls the camera feed as `rtsp://<drone_ip>:10000/drone_cam` and
re-streams it locally. That's the next piece — say the word and I'll wire
up an ffmpeg-based grabber (the binary is already sitting in the iDronam
install, so same story: no extra downloads) alongside this telemetry tap.

## Troubleshooting

- **Dot stays red / "Connection error: ECONNREFUSED"**: wrong IP/port, or
  the drone's TCP MAVLink server only accepts one client and iDronam
  already has it. Close iDronam and retry, or confirm the IP.
- **Connects but no messages**: some flight-side bridges expect the older
  `REQUEST_DATA_STREAM` message we send, but yours might use
  `MESSAGE_INTERVAL` per-message instead — tell me and I'll add it.
