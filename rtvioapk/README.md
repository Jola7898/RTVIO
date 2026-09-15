# RTVIO Mapper

Android app that captures live video, IMU and GPS from a phone and streams them
over WiFi to a desktop reconstruction engine. The phone does acquisition and
transmission only; the 3D model is built and rendered on the desktop.

- **Package** `com.rtvio.mapper`
- **minSdk** 24 (Android 7.0) · **targetSdk/compileSdk** 34
- **Language** Kotlin · **Build** Gradle 8.2 / AGP 8.2.2 / JDK 17

---

## Building

Debug and release both build clean — no warnings, no errors — and the ten
protocol unit tests pass. Verified against JDK 17.0.20.1 (Temurin), Gradle 8.2,
AGP 8.2.2, Kotlin 1.9.22, Android SDK platform 34 / build-tools 34.0.0.

| Artifact | Size |
|---|---|
| `app-debug.apk` | 6.7 MB |
| `app-release-unsigned.apk` | 2.5 MB (after R8) |

What has **not** been exercised is the app running on real hardware: no phone
has been connected, so the camera pipeline, sensor rates, GPS and the live
socket have never run outside the reasoning that produced them. Compiling is not
the same as working — treat the first on-device run as the real test.

### 1. Prerequisites

- **JDK 17** — AGP 8.x will not run on 11 or 21-only setups.
- **Android SDK** with platform 34 and build-tools 34.x.
- Easiest route: install **Android Studio** (Hedgehog 2023.1.1 or newer), which
  bundles a suitable JDK and SDK manager.

A no-admin alternative, if you would rather not install the IDE, is to unzip
Temurin JDK 17, the Android `commandlinetools`, and Gradle 8.2 into your user
profile, then accept the SDK licences and install the packages:

```bash
sdkmanager --sdk_root=$ANDROID_HOME --licenses < yes.txt   # a file of "y" lines
sdkmanager --sdk_root=$ANDROID_HOME "platform-tools" "platforms;android-34" "build-tools;34.0.0"
```

Redirect the licence answers from a *file*: piping them in fails, because
`sdkmanager` is a JVM child that reads `System.in` directly.

### 2. Generate the Gradle wrapper

If `gradle-wrapper.jar` and the `gradlew` scripts are missing (they are binaries
and cannot be authored by hand), Android Studio offers to create them on first
open, or run once with a system Gradle 8.2+:

```bash
gradle wrapper --gradle-version 8.2
```

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
./gradlew test                     # protocol unit tests
./gradlew installDebug             # to a connected device
```

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

## Testing without a desktop engine

`tools/mock_receiver.py` is a complete reference receiver. It validates the wire
format, prints live rates, and optionally saves frames.

```bash
python tools/mock_receiver.py                    # listen on 0.0.0.0:5555, print rates
python tools/mock_receiver.py --view             # live video window + telemetry overlay
python tools/mock_receiver.py --save-frames out/ # write every JPEG
python tools/mock_receiver.py --advertise        # announce over mDNS (needs `pip install zeroconf`)
```

`--view` needs `opencv-python` and `numpy`. It decodes each frame and draws the
live frame rate, bandwidth, IMU rate with the latest accelerometer and gyroscope
values, and the current GPS fix over the video. Networking runs on a worker
thread so the GUI can keep the main thread, which most GUI toolkits require.

Then in the app: **Settings → Server IP** (or **Find receivers on this network**
if you used `--advertise`) → **Test connection** → back → **START STREAMING**.

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
on-disk schema and documents the seven mismatches between the two — several of
which fail silently rather than raising.

## Wire protocol

One TCP connection carries all three payload types, multiplexed and dispatched
on a leading header byte. **All multi-byte fields are big-endian.** A single
writer thread emits whole packets, so a frame is never interleaved with an IMU
batch.

| Packet | Header | Layout |
|---|---|---|
| Frame | `0xFF` | `i64 timestamp_ms`, `i32 width`, `i32 height`, `i32 jpeg_size`, `u8[jpeg_size]` |
| IMU batch | `0xFE` | `i16 count`, then per sample: `i64 timestamp_ns`, `f32 ax ay az`, `f32 gx gy gz` |
| GPS fix | `0xFD` | `i64 timestamp_ms`, `f64 lat`, `f64 lon`, `f32 altitude_m`, `f32 accuracy_m` |
| Handshake ack (desktop → phone) | `0xAA` | `u32 protocol_version`, `u8 status` (0 = OK) |

The handshake is **optional in practice**: a receiver that simply starts reading
is accepted, and the app reports "connected (no handshake sent)" rather than
refusing to stream. Nothing is ever sent phone → desktop except the three
payload packets.

Canonical definition: `app/src/main/java/com/rtvio/mapper/net/Protocol.kt`, with
byte-level tests in `app/src/test/java/com/rtvio/mapper/ProtocolTest.kt`.

### Clock domains — read this before fusing frames with IMU

The two timestamps are **on different clocks**, exactly as the specification
requires, and this is the thing most likely to silently corrupt a downstream
reconstruction:

- **Frame** and **GPS** timestamps are epoch milliseconds (`System.currentTimeMillis()`,
  `Location.getTime()`). Wall clock. Can jump when NTP corrects it.
- **IMU** timestamps are nanoseconds since boot (`SensorEvent.timestamp`).
  Monotonic. Never jumps, but has no relationship to wall time.

You cannot compare them directly. The desktop must estimate the offset itself.
The cheapest usable estimate is to take, at connection time, the first frame
timestamp and the first IMU timestamp and treat their difference as a constant
offset — `mock_receiver.py` prints exactly this. That is good to roughly the
inter-packet interval, which is adequate for coarse association but **not** for
tight visual-inertial fusion; for that, estimate the offset as an online
parameter in the filter, which is standard practice and also absorbs the
residual capture-to-timestamp latency.

If you control both ends, adding a fourth packet type carrying both clocks read
back to back would settle it properly. That is a protocol change, so it is not
done here.

---

## Architecture

```
MainActivity ──── renders state, owns permissions
     │
StreamingSession ── start/stop sequencing, WiFi watch, event bus
     ├── CameraCapture ──→ FrameEncoder ──→ JPEG
     ├── SensorDataCollector ──→ time-aligned ImuSample batches
     ├── GpsCollector ──→ Location fixes
     └── StreamClient ──→ TCP, queues, reconnect, stats
              └── Protocol ── the byte layouts above
```

| File | Role |
|---|---|
| `net/Protocol.kt` | Packet encoders and the handshake reader |
| `net/StreamClient.kt` | Socket, send queues, backoff, statistics |
| `net/ServerDiscovery.kt` | mDNS browse for `_rtvio._tcp` |
| `capture/CameraCapture.kt` | CameraX preview + analysis pipeline |
| `capture/FrameEncoder.kt` | YUV_420_888 → rotated NV21 → JPEG |
| `sensors/SensorDataCollector.kt` | Accelerometer + gyroscope, time-aligned |
| `sensors/GpsCollector.kt` | GNSS fixes, no-fix warning |
| `service/StreamingSession.kt` | Wires the above together |
| `service/StreamingForegroundService.kt` | Keeps capture alive when backgrounded |
| `data/SettingsManager.kt` | Typed view over SharedPreferences |
| `data/DeviceSpecsCollector.kt` | Everything the phone will report about itself |

### Design decisions worth knowing

**Video drops, control waits.** The video queue holds 10 frames and evicts its
*oldest* entry when full; IMU and GPS are drained ahead of video on every pass.
Congestion therefore costs stale frames, never latency, and never inertial data
— which is the right trade because a dropped frame is recoverable from its
neighbours and a dropped IMU sample is not.

**Blocking writes are the backpressure.** A congested socket blocks in
`write()`. That is intended; the bounded queue in front of it is what absorbs
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

**Preview and streaming have separate lifecycles.** The camera binds whenever
the screen is visible so the operator can frame a shot; JPEG encoding is gated
behind a flag. START flips the flag, so there is no camera-open delay between
the tap and the first frame on the wire.

---

## Settings

| Setting | Default | Range |
|---|---|---|
| Server IP | *(empty)* | manual entry or mDNS discovery |
| Server port | 5555 | 1024–65535 |
| Video resolution | 1080p | 720p, 1080p |
| Video frame rate | 30 | 15, 24, 30 |
| JPEG quality | 70 | 30–90 |
| IMU sample rate | 100 Hz | 50, 100, 200 |
| IMU batch interval | 50 ms | 20, 50, 100 |
| Outdoor mode | on | on (GPS) / off |
| Auto-reconnect | on | on/off |
| Reconnect interval | 5 s | 1–30 (base for 5→10→20→40→60 s backoff) |
| Keep device awake | on | on/off |
| FPS overlay | on | on/off |
| Show statistics | on | on/off |
| Haptic feedback | on | on/off |

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

4. **The status panel reports send latency, not a ping.** A round-trip ping is
   not measurable on this protocol — the desktop speaks exactly once, at
   connect. Adding a heartbeat would break the reference receiver, and opening a
   throwaway TCP connection every second would make a single-client receiver see
   a phantom second client. What is reported instead is the time from a frame
   being captured to its last byte reaching the socket, which is both measurable
   and the number that actually tells an operator the link is the bottleneck.

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

Denying camera blocks streaming and says so. Denying location leaves video and
IMU streaming normally, with GPS off.
