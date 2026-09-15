// iDronam telemetry tap — standalone capture bridge.
//
// Connects directly to the drone/companion computer over TCP and speaks
// MAVLink v2 to it, exactly the way iDronam's own m.p.js does (same
// connect() call, same GCS identity, same REQUEST_DATA_STREAM kickoff).
// It does NOT need iDronam running — this talks to the drone on its own.
//
// Decoded telemetry is re-published two ways for downstream consumers
// (this page, and later your RTVIO pipeline):
//   - Socket.IO events, one per MAVLink message name (HEARTBEAT, ATTITUDE, ...)
//   - GET /api/latest -> JSON snapshot of the most recent value of each

const fs = require("fs");
const path = require("path");
const net = require("net");
const http = require("http");
const { spawn } = require("child_process");
const express = require("express");
const { Server: SocketIOServer } = require("socket.io");
const { mavlink20, MAVLink20Processor } = require("./lib/mavlink20.js");

// ---------------------------------------------------------------- config --
const config = JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8"));

function argVal(flag) {
  const i = process.argv.indexOf(flag);
  return i !== -1 ? process.argv[i + 1] : undefined;
}

const DEVICE_IP = argVal("--ip") || config.device_ip;
const DEVICE_PORT = parseInt(argVal("--port") || config.device_port, 10);
const HTTP_PORT = parseInt(argVal("--http") || config.http_port, 10);
const CAMERA_PORT = parseInt(argVal("--camera-port") || config.camera_port, 10);
const CAMERA_PATH = argVal("--camera-path") || config.camera_path;
const FFMPEG_BIN = path.join(__dirname, "vendor", "ffmpeg.exe");

if (!DEVICE_IP || DEVICE_IP === "CHANGE_ME") {
  console.error("\n[capture-bridge] No drone IP configured.");
  console.error('Edit config.json and set "device_ip" to your drone\'s IP,');
  console.error("or run:  vendor\\node.exe server.js --ip <drone_ip> [--port 14550]\n");
  process.exit(1);
}

// ------------------------------------------------------------- web layer --
const app = express();
app.use(express.static(path.join(__dirname, "public")));

const latest = {};
let connected = false;
let lastDataAt = 0;

app.get("/api/latest", (req, res) => {
  res.json({ connected, last_data_ms_ago: lastDataAt ? Date.now() - lastDataAt : null, messages: latest });
});
app.get("/health", (req, res) => res.json({ ok: true, connected }));

// -------------------------------------------------------- camera (video) --
// Re-encodes the drone's RTSP camera feed (same URL iDronam uses) to MJPEG
// over HTTP, which browsers can play directly from a plain <img> tag as
// multipart/x-mixed-replace -- no client-side video library needed.
app.get("/video.mjpeg", (req, res) => {
  const rtspUrl = `rtsp://${DEVICE_IP}:${CAMERA_PORT}/${CAMERA_PATH}`;
  console.log(`[capture-bridge] Video client connected, starting ffmpeg for ${rtspUrl}`);

  const ff = spawn(FFMPEG_BIN, [
    "-rtsp_transport", "tcp",
    "-i", rtspUrl,
    "-an",
    "-f", "mpjpeg",
    "-q:v", "5",
    "pipe:1",
  ]);

  res.setHeader("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg");
  ff.stdout.pipe(res);
  ff.stderr.on("data", () => {}); // ffmpeg logs progress to stderr; ignore unless debugging

  const cleanup = () => ff.kill("SIGKILL");
  req.on("close", cleanup);
  ff.on("error", (err) => console.error("[capture-bridge] ffmpeg error:", err.message));
  ff.on("exit", (code) => console.log(`[capture-bridge] Video client disconnected (ffmpeg exit ${code})`));
});

const httpServer = http.createServer(app);
const io = new SocketIOServer(httpServer, { cors: { origin: "*" } });

io.on("connection", (socket) => {
  console.log("[capture-bridge] Browser client connected:", socket.id);
  socket.emit("STATE", { connected, latest });
});

// -------------------------------------------------------- MAVLink layer --
// Message names we decode and forward. MAVLink message classes/fields here
// are the public, standard "common" dialect -- same on every MAVLink drone.
const FORWARD_MESSAGES = [
  "HEARTBEAT", "ATTITUDE", "GLOBAL_POSITION_INT", "GPS_RAW_INT", "SYS_STATUS",
  "BATTERY_STATUS", "VFR_HUD", "STATUSTEXT", "HOME_POSITION", "LOCAL_POSITION_NED",
  "DISTANCE_SENSOR", "EKF_STATUS_REPORT", "RC_CHANNELS", "SERVO_OUTPUT_RAW",
  "GIMBAL_DEVICE_ATTITUDE_STATUS", "CAMERA_FEEDBACK", "SCALED_IMU", "RAW_IMU",
  "SCALED_IMU2", "ATTITUDE_QUATERNION", "ALTITUDE", "POWER_STATUS",
];

let mav = null;
let conn = null;
let heartbeatTimer = null;
let reconnectTimer = null;
let bytesReceived = 0;

function isLongLike(v) {
  return v && typeof v === "object" && typeof v.low === "number" && typeof v.high === "number";
}

function extractFields(msgInstance) {
  const out = {};
  (msgInstance.fieldnames || []).forEach((f) => {
    const v = msgInstance[f];
    if (isLongLike(v)) out[f] = v.toString();
    else if (Buffer.isBuffer(v)) out[f] = Array.from(v);
    else out[f] = v;
  });
  return out;
}

function sendRequestDataStream() {
  // Same request iDronam sends: "give me every stream at N Hz".
  const msg = new mavlink20.messages.request_data_stream(0, 0, mavlink20.MAV_DATA_STREAM_ALL, 4, 1);
  conn.write(Buffer.from(msg.pack(mav)));
}

function sendSetMessageIntervals() {
  // Newer autopilots (PX4, recent ArduPilot) ignore the deprecated
  // REQUEST_DATA_STREAM above and expect a per-message SET_MESSAGE_INTERVAL
  // command instead. Send both so either kind of flight stack starts streaming.
  const intervalUs = 250000; // 4 Hz, same rate as the REQUEST_DATA_STREAM call
  FORWARD_MESSAGES.forEach((name) => {
    const msgId = mavlink20["MAVLINK_MSG_ID_" + name];
    if (msgId === undefined) return;
    const cmd = new mavlink20.messages.command_long(
      0, 0, mavlink20.MAV_CMD_SET_MESSAGE_INTERVAL, 0, msgId, intervalUs, 0, 0, 0, 0, 0
    );
    conn.write(Buffer.from(cmd.pack(mav)));
  });
}

function sendHeartbeat() {
  // A GCS is expected to keep sending its own heartbeat; some autopilots/
  // telemetry bridges use this to know a ground station is present.
  const msg = new mavlink20.messages.heartbeat(mavlink20.MAV_TYPE_GCS, mavlink20.MAV_AUTOPILOT_INVALID, 0, 0, 0, 3);
  conn.write(Buffer.from(msg.pack(mav)));
}

function connectToDrone() {
  console.log(`[capture-bridge] Connecting to ${DEVICE_IP}:${DEVICE_PORT} (TCP, MAVLink v2)...`);
  bytesReceived = 0;
  mav = new MAVLink20Processor(null, 255, 1); // sysid 255 (GCS), compid 1 -- same identity iDronam uses

  FORWARD_MESSAGES.forEach((name) => {
    mav.on(name, (m) => {
      const data = extractFields(m);
      latest[name] = data;
      io.emit(name, data);
    });
  });

  conn = net.connect({ host: DEVICE_IP, port: DEVICE_PORT });

  conn.on("connect", () => {
    connected = true;
    io.emit("CONN_STATE", { connected: true });
    console.log("[capture-bridge] TCP connected. Requesting telemetry stream...");
    sendRequestDataStream();
    sendSetMessageIntervals();
    heartbeatTimer = setInterval(sendHeartbeat, 1000);
  });

  conn.on("data", (buf) => {
    if (!bytesReceived) console.log("[capture-bridge] First bytes received from drone (%d bytes).", buf.length);
    bytesReceived += buf.length;
    lastDataAt = Date.now();
    io.emit("MAVLINK_PKT", buf); // raw bytes too, in case you want to parse independently later (e.g. for RTVIO timing)
    try {
      mav.parseBuffer(buf);
    } catch (e) {
      // malformed/partial frame -- the processor buffers internally, safe to ignore
    }
  });

  conn.on("error", (err) => {
    console.error("[capture-bridge] Connection error:", err.message);
  });

  conn.on("close", () => {
    connected = false;
    io.emit("CONN_STATE", { connected: false });
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    console.log("[capture-bridge] Disconnected. Retrying in 3s...");
    reconnectTimer = setTimeout(connectToDrone, 3000);
  });
}

// ------------------------------------------------------------------ boot --
httpServer.listen(HTTP_PORT, "0.0.0.0", () => {
  console.log(`[capture-bridge] Dashboard: http://localhost:${HTTP_PORT}`);
  connectToDrone();
});

process.on("SIGINT", () => {
  console.log("\n[capture-bridge] Shutting down...");
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (conn) conn.destroy();
  process.exit(0);
});
