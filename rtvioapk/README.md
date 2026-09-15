# RTVIO Mapper

Android app that captures video, IMU and GPS from a phone and sends them over
WiFi to the desktop reconstruction engine in `../rtvio` — either live, or as a
take that RTVIO Studio starts and stops remotely, or recorded fully offline and
transferred later. The phone does acquisition and transmission only; the 3D
model is built and rendered on the desktop.

- **Package** `com.rtvio.mapper` · **version** 1.1-studio (versionCode 2)
- **minSdk** 24 (Android 7.0) · **targetSdk/compileSdk** 34
- **Language** Kotlin 1.9.22 · **Build** Gradle 8.2 (wrapper included) / AGP 8.2.2 / JDK 17

---

## Building

Re-verified on 15 Sep 2026 on the project's Windows 11 machine (JDK 17,
Android SDK platform 34): `gradlew test assembleDebug` → **BUILD
SUCCESSFUL**, all **19 unit tests pass** (15 in `ProtocolTest`, 4 in
`FrameSpoolTest`), `app-debug.apk` is 7.2 MB. The release variant
(`app-release-unsigned.apk`, R8-shrunk) measured 2.5 MB in an earlier build and
has not been re-measured since the Studio/offline-recording features landed.

Confirmed on real hardware (vivo I2206, Android 14, and earlier devices):

- **RTVIO Studio remote recording** — the phone connected to `python -m
  rtvio.studio`, takes were started/stopped remotely, and a 93 s take (2,221
  frames at 1080×1920) was saved and reconstructed on the desktop.
- **RECORD LOCALLY + Saved sessions → Transfer** — a 118.9 s outdoor session
  (2,863 frames, 0 dropped, 115 GPS fixes) recorded with no network,
  transferred over WiFi, and reconstructed by `rtvio.vggt_reconstruct
  --from-recording` with no conversion; a 44 s session transferred into RTVIO
  Studio and reconstructed there too.
- **Live streaming** — a 52.9 s session (1,137 frames, 21.5 fps, 4,812 IMU
  samples) to `tools/mock_receiver.py`, and a 101 s session straight into a
  live reconstruction via `rtvio.vggt_live` (579 frames, 11 windows, real GPS).
  That run showed a real receiver-side tradeoff: `vggt_live` blocks the socket
  while each window runs, so the app's live video queue drops most frames in
  the meantime (~5.7 fps arrived vs ~25 fps with RECORD LOCALLY). Not an app
  bug — see `../rtvio/README.md` "Live VGGT reconstruction".

The reconstruction *quality* from the first RECORD LOCALLY test was poor, traced
to capture technique (panning in place rather than moving through the scene —
see the root README's field workflow), not to the record/transfer path.

Not yet verified on-device: sustained (10+ minute) thermal behaviour, and the
older `rtvio.live_pipeline --record` path.

### 1. Prerequisites

- **JDK 17** — AGP 8.x will not run on 11 or 21-only setups.
- **Android SDK** with platform 34 and build-tools 34.x.
- Easiest route: install **Android Studio** (Hedgehog 2023.1.1 or newer), which
  bundles a suitable JDK and SDK manager.

A no-admin alternative, if you would rather not install the IDE, is to unzip
Temurin JDK 17 and the Android `commandlinetools` into your user profile, then
accept the SDK licences and install the packages:

```bash
sdkmanager --sdk_root=$ANDROID_HOME --licenses < yes.txt   # a file of "y" lines
sdkmanager --sdk_root=$ANDROID_HOME "platform-tools" "platforms;android-34" "build-tools;34.0.0"
```

Redirect the licence answers from a *file*: piping them in fails, because
`sdkmanager` is a JVM child that reads `System.in` directly.

If `java -version` does not report 17, point `JAVA_HOME` at the JDK 17 folder
before running Gradle.

### 2. Gradle wrapper

The wrapper (`gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.jar`,
pinned to Gradle 8.2) is committed, so there is nothing to generate: the first
`gradlew` run downloads Gradle 8.2 itself.

### 3. Point the build at your SDK

Create `local.properties` in this directory (it is gitignored):

```properties
sdk.dir=C:/Users/<you>/AppData/Local/Android/Sdk
```

Forward slashes on purpose: a `.properties` file treats a lone backslash as an
escape character, so a Windows path pasted in raw silently resolves to
`C:UsersyouAppData...` and the build fails with a confusing "SDK location not
found".

Android Studio writes this for you on first open.

### 4. Build

```bash
./gradlew assembleDebug            # app/build/outputs/apk/debug/app-debug.apk
./gradlew test                     # unit tests (ProtocolTest, FrameSpoolTest)
./gradlew installDebug             # to a connected device (USB debugging on)
```

On Windows use `.\gradlew.bat` in place of `./gradlew`.

### Release signing

The release variant has R8 and resource shrinking on but **no signing config**,
so `assembleRelease` produces an unsigned APK. To sign, create
`keystore.properties` (gitignored):

```properties
storeFile=/path/to/release.keystore
storePassword=...
keyAlias=...
keyPassword=...
```

and add a `signingConfigs` block reading it in `app/build.gradle`. It is left
out deliberately rather than stubbed with placeholders that would fail at the
end of a long build.

---

## Using the app

Set **Settings → Server IP / Server port** to the desktop running the receiver
(default port 5555). The main screen checks every few seconds, with a bare TCP
connect (`net/ReceiverProbe.kt`), whether anything is listening there, and
offers:

| Button | When | What it does |
|---|---|---|
| **CONNECT** | a receiver answers at Server IP | Opens the connection. What happens next depends on the receiver (below). Reads **CANCEL** while connecting, then **DISCONNECT** (RTVIO Studio) or **STOP STREAMING** (any other receiver). |
| **RECORD LOCALLY** | not connected (always offered) | Records to phone storage with no network at all; becomes **■ STOP RECORDING**. |
| **● REC / ■ STOP** | connected to RTVIO Studio | Starts/stops a take from the phone itself — the same as the Studio page's Start/Stop recording. Shows **UPLOADING n** while the phone sends frames it spooled. |
| ☰ → **Saved sessions** | any time | Lists RECORD LOCALLY sessions: **Transfer** or **Delete**. |

What CONNECT does is decided by the version the desktop greets with (see
[Wire protocol](#wire-protocol)):

- **RTVIO Studio** (`python -m rtvio.studio`, greets with protocol v2): the
  phone connects *armed* — camera preview on, a low-rate viewfinder image sent
  to the Studio page, nothing recorded. Takes are started and stopped from the
  Studio page or the phone's ● REC button. While recording, **every frame is
  kept**: frames go to an on-phone spool (`net/FrameSpool.kt`) and on to the
  desktop, and if WiFi falls behind or drops, the spool holds them and uploads
  after STOP. The Studio's START command also carries the capture settings
  (resolution, fps, JPEG quality, whether to record GPS/IMU) and the phone saves
  them into its own settings.
- **Any other receiver** (`rtvio.vggt_live`, `rtvio.live_pipeline`,
  `tools/mock_receiver.py`, greeting v1 or none): the original live stream —
  frames flow from the moment of connect, and when the link lags the oldest
  queued frame is dropped.

## Recording fully offline

**RECORD LOCALLY** needs no network at all: camera, IMU and GPS are captured
straight to the phone's own storage
(`Android/data/com.rtvio.mapper/files/recordings/<id>/`) in exactly the
directory layout `rtvio.vggt_reconstruct --from-recording` reads -
`frames/%06d.jpg` + `frame_timestamps.json`, plus `gps_data.json`,
`imu_data.json`, `camera_intrinsics.json` and `session_meta.json` alongside it
(`capture/LocalSessionRecorder.kt`). This is the answer to "I'm flying
somewhere with no WiFi route back to the desktop at all": record the whole
survey with the phone alone, then deal with getting the data off it once
you're back in range of the receiver.

**Saved sessions** (toolbar menu) lists everything RECORD LOCALLY has saved -
tap one for **Transfer** (sends the whole session to Settings -> Server IP
over its own one-shot TCP connection - see `net/SessionTransferClient.kt` and
`Protocol.HEADER_SESSION_BEGIN`) or **Delete**. Two desktop receivers accept a
transfer:

- **RTVIO Studio** — saves it to `rtvio/data/sessions/<id>/`, lists it under
  Sessions, and reconstructs it automatically if "reconstruct phone takes
  automatically" is ticked.
- **`tools/mock_receiver.py --sessions-dir DIR`** — the reference receiver;
  writes `DIR/<id>/` and prints the `--from-recording` command to run next.

Deleting after a successful transfer is offered, never automatic - your only
copy stays on the phone until you say otherwise.

No USB/adb step is needed for any of this: Transfer reuses the same WiFi link
as live streaming, just at a later time. A file manager or MTP browse of
`Android/data/com.rtvio.mapper/files/recordings/` is a manual fallback if you
would rather pull the folder off over USB yourself, though scoped storage
hides `Android/data` from most apps on Android 11+ (the system's own MTP
service is generally still able to show it - test your device / OS version
if that path matters to you rather than assuming it).

---

## Testing without a desktop engine

`tools/mock_receiver.py` is a complete reference receiver (protocol v1, so the
app streams as soon as it connects). It validates the wire format, prints live
rates, and optionally saves frames.

```bash
python tools/mock_receiver.py                    # listen on 0.0.0.0:5555, print rates
python tools/mock_receiver.py --view             # live video window + telemetry overlay
python tools/mock_receiver.py --save-frames out/ # write every JPEG
python tools/mock_receiver.py --advertise        # announce over mDNS (needs `pip install zeroconf`)
python tools/mock_receiver.py --sessions-dir out/ # accept "Transfer" from Saved sessions (default: ./received_sessions)
```

`--view` needs `opencv-python` and `numpy`. It decodes each frame and draws the
live frame rate, bandwidth, IMU rate with the latest accelerometer and gyroscope
values, and the current GPS fix over the video. Networking runs on a worker
thread so the GUI can keep the main thread, which most GUI toolkits require.

Port 5555 is also RTVIO Studio's phone port: stop the Studio (or run the
receiver with `--port` and set the phone to match) before starting this.

Then in the app: **Settings → Server IP** (or **Find receivers on this network**
if you used `--advertise`) → **Test connection** → back → **CONNECT**.

Typical output:

```
Client connected: 192.168.1.42:41288
  first IMU:   t=12345678000 ns (since boot)  accel=(0.1, 0.2, 9.81)  gyro=(0.01, -0.02, 0.03)
  first frame: 1080x1920, 142 KB, ts=1788766172659
    29.8 fps |   100.2 IMU Hz |   4.31 Mbps | frames   1,234 | imu   12,340 | gps 41
```

If you write your own receiver, read the note at the top of `mock_receiver.py`
about `recv` returning short reads. It is the one mistake that works perfectly
on localhost and fails the moment a real 200 KB frame crosses WiFi.

---

## Integrating with the reconstruction pipeline

If you are wiring this stream into the Python pipeline in `rtvio/`, read
`../rtvio/docs/INTEGRATION.md` first. It maps every packet field onto the pipeline's
on-disk schema and documents the mismatches between the two — several of
which fail silently rather than raising.

## Wire protocol

One TCP connection carries everything, multiplexed and dispatched on a leading
header byte. **All multi-byte fields are big-endian.** A single writer thread
emits whole packets, so a frame is never interleaved with an IMU batch.

| Packet | Header | Direction | Layout |
|---|---|---|---|
| Handshake ack | `0xAA` | desktop → phone | `u32 protocol_version`, `u8 status` (0 = OK) |
| Frame | `0xFF` | phone → desktop | `i64 timestamp_ms`, `i32 width`, `i32 height`, `i32 jpeg_size`, `u8[jpeg_size]` |
| IMU batch | `0xFE` | phone → desktop | `i16 count`, then per sample: `i64 timestamp_ns`, `f32 ax ay az`, `f32 gx gy gz` |
| GPS fix | `0xFD` | phone → desktop | `i64 timestamp_ms`, `f64 lat`, `f64 lon`, `f32 altitude_m`, `f32 accuracy_m` |
| Camera intrinsics | `0xFC` | phone → desktop | `f32 fx fy cx cy`, `f64 k1 k2 p1 p2 k3`, `u16 source_len`, `u8[source_len]` |
| Status *(v2)* | `0xFB` | phone → desktop | `u16 len`, UTF-8 JSON: state, session, counters, battery, clocks — about once a second |
| Preview *(v2)* | `0xFA` | phone → desktop | same layout as Frame; a low-quality viewfinder image while armed, never recorded |
| Command *(v2)* | `0xC0` | desktop → phone | `u16 len`, UTF-8 JSON: `{"cmd": "start" \| "stop" \| "ping", ...}` |
| Session transfer begin | `0xE0` | phone → desktop | `u16 idLen`, `u8[idLen] id`, `i32 fileCount`, `i64 totalBytes` |
| Session transfer file | `0xE1` | phone → desktop | `u16 pathLen`, `u8[pathLen] relPath`, `i64 fileSize`, then `u8[fileSize]` raw |
| Session transfer end | `0xE2` | phone → desktop | *(no payload)* |

**Versions.** A receiver that greets with **version 2** (RTVIO Studio) gets the
remote-control behaviour: STATUS/PREVIEW from the phone, COMMAND from the
desktop, frames only between START and STOP. **Version 1**, or no greeting at
all, gets the original stream: only Frame/IMU/GPS/Intrinsics, from the moment
of connect, and the desktop never sends another byte. The handshake is
optional in practice: a receiver that simply starts reading is accepted, and
the app reports "connected (no handshake sent)". A session transfer
(Saved sessions → Transfer) is always a *separate* connection carrying only the
three transfer packets.

Canonical definition: `app/src/main/java/com/rtvio/mapper/net/Protocol.kt`, with
byte-level tests in `app/src/test/java/com/rtvio/mapper/ProtocolTest.kt`; the
Python side is `../rtvio/src/rtvio/stream/protocol.py`.

### Clock domains — read this before fusing frames with IMU

The timestamps are **on different clocks**, and this is the thing most likely to
silently corrupt a downstream reconstruction:

- **Frame** and **GPS** timestamps are epoch milliseconds (`System.currentTimeMillis()`,
  `Location.getTime()`). Wall clock. Can jump when NTP corrects it. Frames are
  stamped with the sensor exposure time converted to wall clock, not the moment
  encoding finished.
- **IMU** timestamps are nanoseconds since boot (`SensorEvent.timestamp`).
  Monotonic. Never jumps, but has no relationship to wall time.

With **RTVIO Studio (v2)** this is settled on the wire: every STATUS packet
carries `wall_ms` and `elapsed_ns` read back to back on the phone, and the
Studio uses them to convert IMU times onto the frames' clock when it saves a
take. A **v1** receiver has to estimate the offset itself. The cheapest usable
estimate is the difference between the first frame timestamp and the first IMU
timestamp — `mock_receiver.py` prints exactly this — which is good to roughly
the inter-packet interval: adequate for coarse association, **not** for tight
visual-inertial fusion (`rtvio/src/rtvio/stream/clock.py` does better with a
minimum filter over receive times).

---

## Architecture

```
MainActivity ──── renders state, owns permissions, polls receiver reachability
     │
StreamingSession ── link state machine (OFF/CONNECTING/ARMED/RECORDING/FINISHING/STREAMING),
     │                commands from the Studio, WiFi watch, event bus
     ├── CameraCapture ──→ FrameEncoder ──→ JPEG
     ├── SensorDataCollector ──→ time-aligned ImuSample batches
     ├── GpsCollector ──→ Location fixes
     ├── LocalSessionRecorder ──→ RECORD LOCALLY, to phone storage
     └── StreamClient ──→ TCP, queues, FrameSpool, reconnect, stats
              └── Protocol ── the byte layouts above
```

| File | Role |
|---|---|
| `net/Protocol.kt` | Packet encoders, handshake and command readers |
| `net/StreamClient.kt` | Socket, send queues, spool draining, backoff, statistics |
| `net/FrameSpool.kt` | Disk-backed FIFO: a Studio take never drops a frame |
| `net/ReceiverProbe.kt` | "Is anything listening at Server IP?" |
| `net/SessionTransferClient.kt` | Saved sessions → Transfer |
| `net/ServerDiscovery.kt` | mDNS browse for `_rtvio._tcp` |
| `capture/CameraCapture.kt` | CameraX preview + analysis; parallel encode, in-order delivery |
| `capture/FrameEncoder.kt` | YUV_420_888 → rotated NV21 → JPEG |
| `capture/LocalSessionRecorder.kt` | RECORD LOCALLY session writer |
| `sensors/SensorDataCollector.kt` | Accelerometer + gyroscope, time-aligned |
| `sensors/GpsCollector.kt` | GNSS fixes (GPS provider only, not fused), no-fix warning |
| `service/StreamingSession.kt` | Wires the above together |
| `service/StreamingForegroundService.kt` | Keeps capture alive when backgrounded |
| `data/CameraIntrinsics.kt` | K for the frames actually sent (Camera2 calibration, mapped through crop + rotation) |
| `data/SettingsManager.kt` | Typed view over SharedPreferences |
| `data/LocalSessions.kt` | Lists / deletes saved offline sessions |
| `data/DeviceSpecsCollector.kt` | Everything the phone will report about itself |
| `ui/RecordingsActivity.kt` | The Saved sessions screen |

### Design decisions worth knowing

**Two policies for frames.** Live streaming (a v1 receiver) holds 10 frames and
evicts its *oldest* entry when full — for a live view a fresh frame beats a
stale one. A Studio take appends every frame to `FrameSpool` on flash instead
and never drops one; the writer drains it in order, through WiFi stalls and
reconnects, and keeps going after STOP until the backlog is gone. In both
cases IMU, GPS and status are drained ahead of video on every pass.

**Blocking writes are the backpressure.** A congested socket blocks in
`write()`. That is intended; the queue or spool in front of it is what absorbs
the stall. Closing the socket is what unblocks it, and is how both shutdown and
reconnect work.

**The gyroscope is interpolated onto accelerometer timestamps.** The wire format
wants one timestamp carrying both sensors, but Android delivers them
independently with drifting phase. Pairing each accelerometer sample with the
most recent gyroscope reading — the obvious approach — leaves the gyroscope up
to a full sample period stale: 10 ms at 100 Hz, which during a 90°/s pan is a
0.9° attitude error injected into *every* sample for a filter to integrate into
drift. So accelerometer events are held until a bracketing gyroscope sample
arrives and the angular rate is linearly interpolated to the accelerometer's
timestamp. Cost: one sample period of latency. See `SensorDataCollector`.

**Rotation happens during the NV21 assembly.** The frame must be copied out of
three hardware planes into one contiguous buffer anyway, so that copy writes to
rotated destination offsets. The conventional "convert, then rotate" costs an
extra full pass and a second 3 MB buffer per frame at 1080p.

**Preview and recording have separate lifecycles.** The camera binds whenever
the screen is visible so the operator can frame a shot; JPEG encoding is gated
behind a flag. START flips the flag, so there is no camera-open delay between
the tap (or the Studio's command) and the first frame on the wire.

---

## Settings

| Setting | Default | Range |
|---|---|---|
| Server IP | *(empty)* | manual entry or mDNS discovery |
| Server port | 5555 | 1024–65535 |
| Video resolution | 720p | 720p, 1080p (1280 / 1920 px long edge) |
| Aspect ratio | 4:3 | 4:3 (full sensor field of view), 16:9 (cropped) |
| Video frame rate | 30 | 15, 24, 30 |
| JPEG quality | 85 | 30–95 |
| IMU sample rate | 100 Hz | 50, 100, 200 |
| IMU batch interval | 50 ms | 20, 50, 100 |
| Outdoor mode | on | on (GPS) / off |
| Auto-reconnect | on | on/off |
| Reconnect interval | 5 s | 1–30 (base for 5→10→20→40→60 s backoff) |
| Keep device awake | on | on/off |
| FPS overlay | on | on/off |
| Show statistics | on | on/off |
| Haptic feedback | on | on/off |

720p and 4:3 are the defaults on purpose: the desktop's VGGT resizes every frame
to 518 px anyway, so a bigger frame only costs encode time and WiFi bandwidth,
while 4:3 keeps the sensor's full field of view (more overlap between frames).

A take started from **RTVIO Studio** uses the Studio page's Capture settings
(resolution, frame rate, JPEG quality, **Record GPS**, Record IMU) instead, and
writes them back into these settings. Outdoor mode still decides GPS for live
streaming and RECORD LOCALLY.

The settings screen shows a live estimated uplink for the current video
configuration, so an unusable combination is visible before a survey rather than
after one.

---

## Performance notes

At 1080p the per-frame cost is roughly 3 MB of plane copy plus a software JPEG
encode. On a budget device (the spec names Snapdragon 650 / 2 GB class) expect
that to land nearer **20–25 fps than 30**. If you need a guaranteed 30 fps on
that hardware, use **720p** — it is under half the pixels and encodes
comfortably inside the frame budget.

Also worth measuring on your own target before committing to a configuration:

- Sustained thermal behaviour over 10+ minutes; JPEG encoding is CPU-bound and
  phones throttle.
- Battery drain with the wake lock held and the screen on.
- Real WiFi uplink. The specs screen reports a usable-uplink estimate at 50% of
  the negotiated PHY rate, which is the realistic planning figure for TCP over
  802.11 — the negotiated rate itself is not achievable.

---

## Deviations from the original specification

Each of these is a deliberate change, not an omission.

1. **mDNS uses the platform `NsdManager`, not the suggested libraries.** The
   spec proposed `org.bouncycastle` (a crypto provider) and
   `com.github.ServiceComb:java-chassis` (a microservice framework). Neither
   implements mDNS. `NsdManager` does, ships with the OS, and adds nothing to
   the APK.

2. **`androidx.legacy:legacy-support-v4` dropped.** It was listed "for
   logging"; logging is `android.util.Log`, which is in the platform.

3. **CameraX rather than hand-rolled Camera2.** `camera-camera2` *is* the
   Camera2 backend, so this still satisfies "Camera2, not the deprecated
   Camera1", while CameraX handles the session lifecycle and per-device quirks
   that make raw Camera2 the largest single source of crashes in apps like this.

4. **The status panel reports send latency, not a ping.** A v1 receiver speaks
   exactly once, at connect, so a round-trip ping is not measurable against it;
   adding a heartbeat would break the reference receiver. What is reported
   instead is the time from a frame being captured to its last byte reaching
   the socket, which is both measurable and the number that tells an operator
   the link is the bottleneck.

5. **A foreground service was added.** Not in the spec, but without one Android
   stops the capture pipeline as soon as the screen turns off or the user
   switches apps — which would end a survey silently.

6. **Localisation was not done.** Listed as a nice-to-have; all user-facing text
   is externalised in `res/values/strings.xml`, so adding `values-hi` and the
   rest is a translation task with no code changes.

---

## Permissions

Requested at runtime: `CAMERA` (required), `ACCESS_FINE_LOCATION` (outdoor mode,
and what lets Android report the WiFi SSID), `POST_NOTIFICATIONS` (Android 13+,
for the foreground-service notification).

Declared: `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`,
`CHANGE_NETWORK_STATE`, `CHANGE_WIFI_MULTICAST_STATE`, `FOREGROUND_SERVICE`
(+ `_CAMERA`, `_LOCATION`, `_DATA_SYNC`), `WAKE_LOCK`, `VIBRATE`.

Denying camera blocks capture and says so. Denying location leaves video and
IMU working normally, with GPS off.
