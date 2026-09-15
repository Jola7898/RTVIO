# RTVIO — integrating the live phone stream into the reconstruction pipeline

**Audience:** the next model or engineer picking up this work.
**Task:** feed the live video + IMU + GPS stream from the Android app into the
existing Python reconstruction pipeline.
**Written:** 7 September 2026.

Read this top to bottom before writing code. It exists specifically so you do
not rediscover the seven mismatches in §4 the hard way — several of them fail
*silently*, producing a reconstruction that looks plausible and is wrong.

---

## 1. The two halves, and what is actually proven

### Producer — `rtvioapk/` (Android app, Kotlin)

An Android app that captures camera + IMU + GPS and streams them over TCP.

| | |
|---|---|
| Compiles | ✅ clean, zero warnings (AGP 8.2.2, Kotlin 1.9.22, SDK 34) |
| Wire format | ✅ 10 byte-level unit tests, plus validated end-to-end against `rtvioapk/tools/mock_receiver.py` |
| Run on a real phone | ❌ **never** |

That last row matters. The camera pipeline, sensor rates, GPS and the live
socket have never executed on hardware. Do not assume any measured number
(frame rate, latency, IMU jitter) until you have seen it on a device.

### Consumer — `rtvio/` (Python pipeline)

**`rtvio/` is the current pipeline.** `rtvio_3/` contains only
`PROJECT_CONTEXT.md` — a rewrite-from-scratch plan whose status section
("none of D1–D8 has working Python code yet") predates `rtvio/`'s code.
Do not treat `rtvio_3/` as the implementation. (The old MATLAB/Blender demo
that used to live at `rtvio/` has been removed; that name now refers to the
Python pipeline, formerly `rtvio_2/`.)

Entry point:

```bash
python pipeline.py [dataset_dir] [--live-viz] [--run-id NAME] [--cell-size-m 1.0]
```

Stages: ingest → EKF + sparse tracking → georeference → dense stereo →
meshing → export → evaluate.
Outputs into `output_<run-id>/`: `pose_file.csv`, `cloud.las`, `mesh.obj`,
`mesh_origin.json`, `dsm.png`.

**Its shape is fundamentally batch.** `ingest.Dataset.__init__` eagerly decodes
the entire video and loads every JSON into memory, and
`pipeline.run_ekf_and_tracking` is a single `for sample in ds.imu:` loop that
interleaves GPS and frames by timestamp. There is no incremental entry point.

### Scope of this document

I read `ingest.py` in full, `pipeline.py`'s `run_ekf_and_tracking` and `main`,
and the four input JSON schemas, and grepped for every consumer of the
ground-truth fields and config keys. I have **not** read `tracking.py`,
`dense_stereo.py`, `inertial_nav_ekf.py`, `georeference.py`, `meshing.py`,
`export.py` or `evaluate.py` internals. Claims about those are inferred from
their call sites and are marked where it matters.

---

## 2. Do this first: record to disk, do not stream

**Recommended Phase 1: write a session recorder that saves the phone stream to
disk in exactly the schema `Dataset` already reads, then run `pipeline.py`
unchanged.**

Why this order:

- It isolates the two failure domains. When the reconstruction is wrong, you
  need to know whether the data is bad or the pipeline is. A recorded session
  is inspectable, replayable and diffable; a live stream is none of those.
- Every stage after the EKF is inherently non-real-time. Dense stereo is
  labelled "the slow stage" in `pipeline.py` and the dense cloud needs
  `refined_poses` from a *windowed bundle adjustment* over the whole sequence.
  A live 3D model is not a small change to this code; it is a different
  architecture.
- The recorder is ~150 lines on top of `mock_receiver.py`, which already parses
  the protocol correctly.
- It gives you regression fixtures. Record one good session and you can iterate
  on the pipeline for days without touching a phone.

Phase 2 (genuine streaming) is discussed in §7. Do not start there.

---

## 3. Schema mapping: app packet → pipeline input

The protocol is defined in `rtvioapk/app/src/main/java/com/rtvio/mapper/net/Protocol.kt`
and documented in `rtvioapk/README.md` § *Wire protocol*. All fields are
**big-endian**. Parsing already exists in `rtvioapk/tools/mock_receiver.py` —
reuse `recv_exact`, `read_frame`, `read_imu`, `read_gps` rather than rewriting.

### Files `Dataset` expects in `dataset_dir`

| File | Required? | How to produce it from the stream |
|---|---|---|
| `flight.mp4` | yes | Encode received JPEGs to H.264, **or** patch `ingest.load_video_frames` to read a JPEG directory (see §4.7) |
| `camera_intrinsics.json` | yes | **Calibrate.** See §4.5 — do not guess |
| `flight_config.json` | yes | Assemble at end of session; see below |
| `imu_data.json` | yes | From `0xFE` packets |
| `gps_data.json` | yes | From `0xFD` packets |
| `gt_trajectory.json` | **crashes without** | Does not exist for a real capture — see §4.3 |
| `checkpoints.json` | **crashes without** | Same |

### Frame packet `0xFF` → video + timing

| Wire field | Type | Destination |
|---|---|---|
| `timestamp_ms` | `int64`, **epoch ms** | Frame timing — see §4.1, §4.2 |
| `width`, `height` | `int32` | Must match `camera_intrinsics.json` exactly |
| `jpeg_size` + payload | `int32` + bytes | A frame of `flight.mp4`, or `frames/NNNNNN.jpg` |

### IMU batch `0xFE` → `imu_data.json`

Wire, per sample: `int64 timestamp_ns` (**nanoseconds since boot**), then
`float32 ax, ay, az` (m/s²), `float32 gx, gy, gz` (rad/s).

```json
{
  "timestamp": 0.0,
  "accel_body_xyz": [ax, ay, az],
  "gyro_body_xyz":  [gx, gy, gz]
}
```

- `timestamp` is **float seconds, zero at session start** — not the wire value.
  Convert: `(t_ns - t0_ns) / 1e9`.
- `true_accel_bias_xyz` / `true_gyro_bias_xyz` appear in the synthetic file but
  are **ground-truth only**. I grepped: nothing outside the dataset generator
  reads them. Omit them.
- **Accelerometer includes gravity.** The app uses `TYPE_ACCELEROMETER`, not
  `TYPE_LINEAR_ACCELERATION`. `ekf.initialize_attitude(accel_body_xyz)` relies
  on that, so this is the correct pairing — but if anyone "improves" the app to
  send linear acceleration, attitude initialisation breaks silently.
- The app already **interpolates the gyroscope onto each accelerometer
  timestamp**, so every sample is genuinely time-aligned. You do not need to
  re-align. See `SensorDataCollector`'s class comment for why.

### GPS packet `0xFD` → `gps_data.json`

Wire: `int64 timestamp_ms` (epoch ms), `float64 lat`, `float64 lon`,
`float32 altitude_m`, `float32 accuracy_m` (−1 when unknown).

```json
{
  "timestamp": 0.0,
  "latitude_deg": 12.97,
  "longitude_deg": 77.59,
  "altitude_m": 920.5,
  "enu_xyz_noisy": [e, n, u],
  "num_satellites": 11,
  "hdop": 1.53
}
```

- **`enu_xyz_noisy` is the field the EKF actually consumes** —
  `ekf.update_gps(ds.gps[i]["enu_xyz_noisy"], ...)`. Lat/lon are carried but not
  used for fusion. You must compute ENU yourself (§4.4).
- `num_satellites` and `hdop` are **not in the protocol**. Nothing in the
  pipeline reads them (grepped). Omit, or synthesise from `accuracy_m`.
- `accuracy_m` is strictly more useful than `hdop` — it is a metre-scale 1-sigma
  estimate. Use it per-fix instead of the single global
  `config["gps_noise_std_m"]`; see §4.4.

### `flight_config.json`

Read by the pipeline: `fps`, `total_time_s`, `ref_lat_deg`, `ref_lon_deg`,
`ref_alt_m`, `gps_noise_std_m`. (`imu_hz`, `gps_hz`, `n_frames` and the
flight-geometry keys are generator inputs; grep before assuming a consumer.)

`fps` and `total_time_s` are only knowable once the session ends — another
reason the recorder writes this file last.

---

## 4. The seven mismatches, worst first

### 4.1 Three different clocks — the highest-risk item

| Source | Clock | Epoch |
|---|---|---|
| Frame `timestamp_ms` | wall clock (`System.currentTimeMillis`) | Unix epoch, ms |
| GPS `timestamp_ms` | wall clock (`Location.getTime`) | Unix epoch, ms |
| IMU `timestamp_ns` | **monotonic since boot** (`SensorEvent.timestamp`) | device boot, ns |
| Pipeline | float seconds | **0.0 at session start** |

The IMU clock has no defined relationship to the other two. Subtracting them
naively yields an offset of ~10¹² — `mock_receiver.py` prints exactly this at
disconnect, and it is a useful sanity check, not a usable calibration.

This split is deliberate (a monotonic IMU clock does not jump when NTP corrects
the wall clock) and is documented in `Protocol.encodeImuBatch`.

**Minimum viable fix for the recorder:** on the first frame and first IMU
sample, record both; treat `offset = first_frame_ms/1000 − first_imu_ns/1e9` as
constant; convert everything to session-relative seconds with a single shared
`t0`.

**Why that is not good enough for tight fusion:** the estimate is only accurate
to roughly one inter-packet interval (tens of ms), and it silently absorbs
capture-to-timestamp latency. Tens of ms of camera/IMU misalignment at walking
pace is centimetres of position error injected into every frame, and it will
show up as scale and drift error you cannot tune away. **Estimate the offset as
an online parameter in the EKF** — this is standard VIO practice. Until then,
treat absolute accuracy claims with suspicion.

If you control both ends, the clean fix is a new packet type carrying
`System.currentTimeMillis()` and `SystemClock.elapsedRealtimeNanos()` read back
to back. That is a protocol change; it is not done, and it would need matching
work in `Protocol.kt`, `StreamClient.kt` and every receiver.

### 4.2 `frame_timestamp()` assumes a perfectly constant frame rate

```python
# ingest.py:75
def frame_timestamp(self, frame_idx):
    return frame_idx / self.config["fps"]
```

Frame *time* is derived from frame *index*. That holds for a synthetic dataset.
It does not hold for a live capture:

- The app's `StreamClient` **drops frames on purpose** when the link congests —
  bounded 10-frame queue, oldest evicted (`framesDropped` is reported).
- Real capture jitters; the software rate gate has a deliberate 10% tolerance.
- Reconnects after a WiFi drop leave a gap of arbitrary length.

One dropped frame shifts every subsequent frame's timestamp by 1/fps and
**every later stage inherits the error** — EKF/vision interleaving, the
`georeference_trajectory(timestamps, ...)` call, and the pose CSV.

This will not raise an exception. It will produce a smoothly wrong trajectory.

Two options:

1. **Carry real per-frame timestamps** (preferred). Write a
   `frame_timestamps.json` alongside, and change `frame_timestamp` to index it.
   Grep for `frame_timestamp` and `config["fps"]` first — `pipeline.py`'s
   georeference stage builds `timestamps` from it, and `dense_stereo`/`tracking`
   may assume index-time equivalence.
2. **Resample to a fixed grid** at record time — duplicate or drop frames so
   index/time equivalence genuinely holds. Cruder, but zero pipeline change, and
   it keeps `ds.blurred`, `ds.frames` and pose lists index-aligned, which
   `ingest.py`'s comment stresses is load-bearing.

Whichever you choose, **record `framesDropped` in the session metadata** so a
reconstruction can be audited afterwards.

### 4.3 `Dataset.__init__` hard-requires ground-truth files

```python
# ingest.py:54-55
self.gt_trajectory = load_json(os.path.join(root_dir, "gt_trajectory.json"))
self.checkpoints   = load_json(os.path.join(root_dir, "checkpoints.json"))
```

A real phone capture has neither. `Dataset` raises before the pipeline starts.

Make both optional (`None` when absent) and guard the consumers. From grepping,
they are used only for evaluation:

- `pipeline.py:249-260` — ATE/RPE against `gt_trajectory`
- `pipeline.py:271-275` — checkpoint RMSE

So deliverable **D7 (accuracy report) is not achievable from a phone capture
alone** — there is nothing to score against. Either survey real checkpoints
with better-than-GNSS accuracy, or accept that live captures produce a model
with no accuracy claim and keep the synthetic dataset as the accuracy fixture.
State this explicitly rather than letting a "0.00 m RMSE" appear because an
empty list was compared against itself.

### 4.4 GPS: lat/lon → ENU, and the reference origin

The EKF consumes local ENU metres. You need `lat/lon/alt → ENU` about a fixed
origin, and that origin must be the same one `pipeline.py` reads as
`config["ref_lat_deg"] / ref_lon_deg / ref_alt_m` and passes to
`georeference_trajectory` and `export_las`. Get them inconsistent and the model
is internally fine but georeferenced to the wrong place.

- Use the **first valid fix** as the origin, and write those exact values into
  `flight_config.json`.
- Prefer a standard geodetic conversion (`pyproj`, WGS84 → local ENU) over a
  hand-rolled flat-earth approximation. Note `georeference.py` already picks an
  EPSG code; read it and match its convention before adding a second one.
- **A known bug in the old MATLAB code was adding reference altitude twice** in
  `enu_to_latlon` (see `rtvio_3/PROJECT_CONTEXT.md` §2.1). Whatever you write,
  round-trip test it: `enu(latlon(enu)) == enu` to millimetres.
- Altitude: the app documents `altitude_m` as metres above the WGS84 ellipsoid.
  Android's `Location.getAltitude()` is ellipsoidal, but **vendor
  implementations vary** and some return orthometric height. A 20–100 m
  systematic Z offset here is very hard to spot downstream. Verify against a
  known elevation before trusting Z.
- Use per-fix `accuracy_m` for the measurement noise instead of the global
  constant. `ekf.update_gps(pos, noise_std)` already takes a per-call sigma, so
  this needs no EKF change — and it matters, because the app streams fixes
  whether accuracy is 3 m or 50 m. Guard the `-1` (unknown) case.

### 4.5 Camera intrinsics: the current file is synthetic

```json
{"fx": 640.0, "fy": 640.0, "cx": 480.0, "cy": 270.0, "width": 960, "height": 540}
```

That is a nominal pinhole for a rendered 960×540 dataset. The phone streams
**720×1280 or 1080×1920, portrait** — different resolution, different aspect,
different axis order, and a real lens with real distortion.

- **You must calibrate.** Checkerboard, OpenCV
  `calibrateCamera`, at the exact resolution the app is configured to stream.
  Re-calibrate if the resolution setting changes.
- `DeviceSpecsCollector` reports focal length in mm and physical sensor size in
  mm, so `fx ≈ f_mm / sensor_width_mm × image_width_px` gives a starting
  estimate. It is a *sanity check on your calibration*, not a substitute — it
  ignores distortion, and phone cameras crop and digitally stabilise.
- Note `cx, cy` in the synthetic file are exactly `width/2, height/2`. A real
  principal point is not centred. Do not preserve that assumption.
- **Distortion is not in the schema at all.** There are no `k1..k3, p1, p2`
  fields, and `ingest.py` builds only a 3×3 `K`. Phone wide lenses have
  meaningful radial distortion. Either undistort frames at record time (then the
  pinhole `K` is honest) or extend the schema and every consumer. Undistorting
  at record time is much less invasive — but do it **before** computing
  intrinsics-dependent anything, and record that you did.

### 4.6 Image rotation vs IMU axes — the subtle one

The app rotates the camera buffer to upright before sending
(`FrameEncoder.encode`, using `ImageInfo.rotationDegrees`). **It does not rotate
the IMU.** IMU samples are in the raw Android device body frame.

So the camera↔IMU extrinsic is *not* identity, and the image axes are not the
sensor axes. Two useful properties, both worth verifying on-device:

1. The activity is **locked to portrait** (`screenOrientation="portrait"`), so
   `targetRotation` is always `ROTATION_0` and `rotationDegrees` is constant for
   a given device. **The extrinsic is therefore constant** and does not change
   with how the operator holds the phone. This is the behaviour you want; don't
   let anyone "fix" the orientation lock.
2. With the delivered image aligned to the device's natural portrait
   orientation, and Android's sensor frame being *x right, y up, z out of the
   screen toward the user*, while the OpenCV camera frame is *x right, y down,
   z along the optical axis (out of the phone's back)*:

   ```
   R_device_from_camera = [[1,  0,  0],
                           [0, -1,  0],
                           [0,  0, -1]]     # 180 deg about X
   ```

**Treat that matrix as a hypothesis to be tested, not a fact.** `SENSOR_ORIENTATION`
is 90 on most back cameras but 270 on some, and front cameras mirror. Verify
empirically: rotate the phone about one axis at a time and check that the sign
of the gyro component matches the observed image motion. Getting this wrong
produces a trajectory that is self-consistent and wrong in a way that looks like
a calibration problem for weeks.

Also: the translation is a few millimetres (lens to IMU die). Small, but not
zero, and it couples with rotation rate. Start with zero, revisit if scale is
off.

### 4.7 Video encoding, and the double-compression tax

`ingest.load_video_frames` uses `cv2.VideoCapture` on `flight.mp4`. Its
docstring makes a deliberate point of reading the *compressed* video so the
pipeline faces compression artifacts.

The phone already sends **JPEG**, i.e. already-lossy frames. Re-encoding them to
H.264 stacks a second generation of loss on top, which directly costs feature
matches and therefore depth quality.

Prefer **patching `load_video_frames` to accept a directory of JPEGs**, keeping
the frames exactly as the phone encoded them. It is a small, contained change to
one function, and it removes a whole error source. If you must produce an mp4
for other tooling, write it *in addition*, not as the pipeline's input.

Also note `ds.blurred` is computed from Laplacian variance against `0.35 ×
median`. That threshold was tuned on synthetic renders. On real handheld phone
footage the blur distribution is completely different — re-check what fraction
of frames it flags before trusting it, because flagging most of the session
would quietly gut the dense stage.

---

## 5. Other things that will bite

- **EKF noise parameters are tuned for synthetic data.**
  `accel_noise_std=0.08, gyro_noise_std=0.004` in `run_ekf_and_tracking`. A
  phone MEMS IMU is substantially noisier and has real bias instability. Expect
  to retune. `DeviceSpecsCollector` reports each sensor's resolution and
  full-scale range on the specs screen — a reasonable starting point, but no
  substitute for an Allan-variance measurement from a few minutes of stationary
  data. **Record a stationary session first**; it is the cheapest calibration
  you will ever get and it also validates the whole recording path.
- **The synthetic IMU is not physically plausible.** `rtvio/imu_data.json`
  sample 2 has `accel_body_xyz ≈ [-0.21, 1015.05, -261.81]` — over 100 g.
  `rtvio_3/PROJECT_CONTEXT.md` flags the old data as having unphysical dynamics.
  Do not use the synthetic magnitudes as a reference for what "normal" looks
  like, and add a plausibility assertion (|accel| within a few g, |gyro| within
  the sensor's full-scale range) to the recorder.
- **GPS rate.** `flight_config.json` says `gps_hz: 5`. Android GPS is typically
  **1 Hz**, and the app requests 1000 ms updates. Grep for `gps_hz` consumers.
- **Handheld portrait vs drone footage.** The pipeline is written for a
  single-pass drone flight at ~80 m altitude and ~10 m/s. A handheld phone walk
  has a totally different baseline distribution, much closer subject depths, and
  rotation the drone case never sees. `dense_stereo.depth_range_from_sparse`
  bounds the depth sweep from the sparse map, which helps, but the 5–250 m
  blanket fallback is drone-scale. Check it.
- **Session length and memory.** `Dataset` holds every decoded frame in RAM. At
  1080×1920 BGR that is ~6 MB/frame — a 60-second capture at 25 fps is ~9 GB.
  This will OOM before anything else does. Cap session length, downsample on
  load (`downsample_intrinsics` exists for exactly this and already scales
  `fx/fy/cx/cy` correctly), or make ingest lazy.
- **The app is portrait-locked and its preview is 4:3-agnostic** — confirm the
  streamed aspect ratio matches what you calibrate. `CameraCapture` logs the
  resolution the camera actually granted, which can differ from the request;
  read that log line rather than assuming the setting.

---

## 6. Suggested build order

1. **Stationary recording.** Extend `mock_receiver.py` into
   `tools/record_session.py`. Phone flat on a table, 60 s. Produces a complete
   `dataset_dir`. Verify: IMU rate matches the setting, |accel| ≈ 9.81, gyro ≈ 0,
   frame timestamps monotonic, `framesDropped == 0` on a good link.
2. **Noise characterisation** from that session → new EKF parameters.
3. **Intrinsics calibration** at the streaming resolution. Cross-check against
   `DeviceSpecsCollector`'s focal-length/sensor-size derivation.
4. **Make `Dataset` tolerate missing ground truth** (§4.3) and read real frame
   timestamps (§4.2).
5. **Extrinsic verification** (§4.6) — the single-axis rotation test.
6. **Short real capture**, slow walk, textured ground, GPS outdoors. Run
   `pipeline.py`. Expect failure; the goal is a *specific* failure.
7. Only then consider streaming (§7).

---

## 7. Phase 2: actual streaming

If a live model is genuinely required, the honest split is:

- **Real-time-able:** EKF propagation and GPS correction, sparse tracking, the
  live pose. These are already causal, per-sample operations.
- **Not real-time-able as written:** windowed bundle adjustment (needs future
  frames), dense stereo, meshing, export. `pipeline.py` explicitly keeps
  `frame_poses` and `refined_poses` separate because BA overwrites the latter in
  place after the fact.

The realistic architecture is a **live pose/trajectory view** streamed back to
the operator while dense reconstruction runs behind it on a lag, keyframe-window
by keyframe-window. That is a real project, not an adaptation.

The app side already supports it: `StreamClient` multiplexes on one socket, the
protocol dispatches on a header byte, and adding a desktop→phone channel would
be a new packet type. Note the app currently **only ever reads the 6-byte
handshake** and never expects another inbound byte
(`StreamClient.readGreeting`), so bidirectional traffic needs a reader loop that
does not exist yet.

---

## 8. Acceptance tests worth writing before you need them

- **Round-trip:** synthetic session → recorder → `Dataset` → assert timestamps
  monotonic, all three streams span the same interval, frame count matches
  `n_frames`.
- **Clock sanity:** IMU, frame and GPS time ranges overlap after conversion. A
  10¹²-scale offset means §4.1 was skipped.
- **Gravity:** stationary session, mean |accel| within 0.2 m/s² of 9.81.
- **ENU round-trip:** `enu → latlon → enu` agrees to <1 mm, and ENU origin
  equals `config["ref_*"]` exactly.
- **Extrinsic:** single-axis rotation test signs match §4.6.
- **Intrinsics:** reprojection RMSE from calibration < 0.5 px, and
  `width/height` in the JSON equal the streamed frame dimensions.
- **Drop resilience:** delete 5% of frames from a recorded session at random;
  the trajectory should degrade gracefully, not shift systematically. If it
  shifts, §4.2 is unfixed.

---

## 9. Quick file reference

| Path | What it is |
|---|---|
| `rtvioapk/README.md` | App architecture, wire protocol, clock-domain notes, deviations |
| `rtvioapk/.../net/Protocol.kt` | Canonical packet definitions |
| `rtvioapk/.../net/StreamClient.kt` | Queues, drop policy, reconnect, stats |
| `rtvioapk/.../sensors/SensorDataCollector.kt` | Gyro→accel interpolation; read the class comment |
| `rtvioapk/.../capture/FrameEncoder.kt` | YUV→NV21→JPEG and the rotation (§4.6) |
| `rtvioapk/.../data/DeviceSpecsCollector.kt` | Focal length, sensor size, FOV, IMU ranges |
| `rtvioapk/tools/mock_receiver.py` | Working receiver + live viewer; **start the recorder here** |
| `rtvio/ingest.py` | `Dataset`; the schema contract you must satisfy |
| `rtvio/pipeline.py` | Orchestrator; `run_ekf_and_tracking` is the fusion loop |
| `rtvio/inertial_nav_ekf.py` | EKF; `update_gps` takes a per-call sigma |
| `rtvio/georeference.py` | EPSG selection, `POSE_FILE_COLUMNS` |
| `rtvio_3/PROJECT_CONTEXT.md` | Deliverables D1–D8, decision records, old-code audit |

---

## 10. The one-paragraph version

Write a recorder on top of `mock_receiver.py` that converts the three packet
streams into `Dataset`'s on-disk schema, and run the existing batch pipeline
unchanged. The three things most likely to silently corrupt your results, in
order, are: the IMU being on a monotonic since-boot clock while frames and GPS
are on wall-clock (§4.1); `frame_timestamp()` deriving time from frame index
when the app deliberately drops frames (§4.2); and the camera↔IMU extrinsic,
because the app rotates the image but not the IMU (§4.6). Calibrate real
intrinsics before believing any geometry, record a stationary session before
trusting any sensor, and remember that no part of the app has yet run on a
physical phone.
