# Live ingest: phone stream → 3D model

The reconstruction is built from packets as they arrive. There is no
record-then-process step in the data path.

```powershell
# start the receiver, then point the app's "Server IP" at this host
python -u -m rtvio.live_pipeline --port 5555 --run-id flight1
```

Tests: `python tests/test_stream.py` (18 checks, ~2 s).

`tools/replay_dataset_as_phone.py` (a test double that streamed a synthetic
dataset over a real socket in place of a phone) and `tools/score_live_run.py`
(scored a run against that dataset's ground truth) have been removed along
with the synthetic dataset itself and the batch `pipeline.py`. The
measurements below were taken while those tools still existed; there is
currently no way to score a run against ground truth in this repo — a real
phone capture has never had any (see §4.3 below), and the synthetic one that
did is gone.

---

## What was measured

A full run, streamed over TCP at true real-time pace from
`tools/replay_dataset_as_phone.py`, against `pipeline.py` on the same
dataset:

| | batch `pipeline.py` | live `live_pipeline.py` |
|---|---|---|
| ATE RMSE (Umeyama-aligned) | 1.352 m | **1.391 m** |
| Georeferenced RMSE (no alignment) | not reported | **1.636 m** |
| Track bearing error | not reported | **0.60°** |
| Dense cloud | 998,190 pts | **1,952,645 pts** |
| Mesh completeness | 32.9 % | **51.0 %** |
| Peak frames held in RAM | 600 (all of them) | **20** |
| Dense-lane latency | n/a (whole-session) | **3.74 s** |

Two of those deserve comment.

**The live path is denser, not sparser.** The batch pipeline's loader (since
removed - it decoded `flight.mp4`) saw H.264 on top of whatever loss the
source already had. The phone sends JPEG and the live path uses those bytes
directly, so there is one generation of compression instead of two. That
difference shows up exactly where INTEGRATION.md §4.7 predicts: in feature
matches, and therefore in depth.

**Memory stops scaling with session length.** The batch loader held every
decoded frame; at 1080×1920 that is ~6 MB/frame, so a 60 s capture at
25 fps is ~9 GB and OOMs before anything else fails. The live path holds
only the frames a pending keyframe still needs.

---

## Why there is a lag, and why it is not a recording

Frame *i*'s depth cannot be computed when frame *i* arrives. Triangulation
needs parallax, parallax needs the camera to have moved, and
`dense_stereo.find_stereo_partners` will not pair views closer than
`MIN_STEREO_BASELINE_M = 4.0`. At 10 m/s that is ~0.4–2 s of flight *after*
frame *i*.

So the pipeline keeps a rolling window of the last few seconds of frames.
Frames enter, get their depth once partners arrive, contribute points, and
are dropped.

| | rolling window | a recording |
|---|---|---|
| lives in | RAM | disk |
| size | bounded, ~20–100 frames | grows with the flight |
| frame's fate | dropped after use | kept |
| model can be built | during the flight | only afterwards |

Three lanes run at three latencies:

| lane | latency | what it produces |
|---|---|---|
| Gyro integration (BA prior only) | ~1 ms | no pose change - see README.md "Pose comes from vision, not IMU" |
| Sparse tracking (solvePnPRansac) + windowed BA | ~0.3 s | the live pose, live map, refined trajectory |
| Dense stereo per keyframe | ~2–5 s | cloud tiles appended to the model |

(This table described an EKF-predict lane before the pose architecture
changed - see CHANGELOG.md "Removed the EKF/IMU-dead-reckoning trajectory".
GPS now re-anchors the pose directly rather than correcting a filter.)

The dense lane runs on worker threads behind a **bounded** queue. When it
cannot keep up it sheds keyframes and says so, rather than growing a queue
until the process dies — for a live system a sparser cloud that tracks the
aircraft beats a dense one that stopped following it. The final
mesh/LAS/DSM write at shutdown is serialisation of a model that was
already built, not a second pass.

---

## The recorder is a test harness

`--record DIR` adds a sibling subscriber that writes a replayable fixture.
It cannot affect the model: it sees the same events on its own thread
behind a bounded queue, drops rather than blocks, and the reconstruction
has no file-reading code path at all. Deleting it changes nothing.

`--replay DIR` drives from such a fixture. It exists because three of the
known hazards fail *silently* — clock domains, frame timing, the
camera↔IMU extrinsic — and fixing a silent bug means changing one thing
and seeing the output move, which is impossible against a stream where
every run is different footage. Replay announces itself on stdout and in
the report. **A model built that way is a model built for debugging.**

Verified: live 1.383 m ATE → recorded → replayed → 1.360 m. The fixture is
faithful.

---

## The hazards, and what was done about each

**§4.1 Three clocks.** IMU is monotonic-since-boot ns; frames and GPS are
epoch ms. `stream/clock.py` estimates the offset with a *minimum filter*
over receive times rather than pairing the first packet of each — transport
delay is positive and jittery, so the minimum over many observations beats
any single pairing. Measured: jitter fully suppressed, residual error equal
to the difference in per-stream capture-to-send latency, which is the
documented limitation. `imu_time_offset_s` is a single mutable number every
conversion reads through, so refining it online is a contained change
wherever it's made. **Not done: the online estimation.** Until it is,
treat absolute accuracy claims with suspicion.

**§4.2 `frame_timestamp()` assumes constant fps.** Dissolved rather than
fixed — the live path carries each frame's own timestamp from the wire and
never derives time from index. Verified by dropping 6.2 % of frames: ATE
1.383 → 1.534 m and bearing 0.58° → 0.57°, i.e. graceful degradation with
**no systematic shift**, which is the §8 criterion.

**§4.3 Ground-truth files.** The live path never constructs the old batch
`Dataset` loader, so it cannot trip over them. The report states plainly
that no accuracy figure is available from a live capture and why, instead
of printing a 0.00 m RMSE from comparing an empty list with itself.

**§4.4 GPS → ENU.** `stream/geodesy.py` is the exact algebraic inverse of
`georeference.enu_to_latlon` — deliberately the same flat-Earth
approximation, not a better one, because two mutually inconsistent
conversions put the model a metre out with no symptom. Round-trip closes to
5×10⁻¹⁰ m. Origin is the first valid fix and is written into
`session_config.json`, so the pose's ENU frame and the georeferencing frame
cannot drift apart. Per-fix `accuracy_m` (clamped both ways by
`gps_sigma_m`) is what `on_gps`'s `MAX_GPS_REANCHOR_SIGMA_M` gate checks
before re-anchoring the pose to a fix - see README.md "No principled
GPS/vision fusion".

**§4.5 Intrinsics.** The first frame's dimensions are checked against
`data/camera_intrinsics.json` and the run *aborts* on a mismatch rather than
silently rescaling the reconstruction. A principal point at exactly the
image centre is flagged as "looks like nominal intrinsics, not a
calibration". **Not done: distortion.** The schema still has no k1..k3/p1/p2
and `K` is still 3×3. Undistort at ingest before trusting phone geometry.

**§4.6 Camera↔IMU extrinsic.** **Not addressed.** The app rotates the image
and not the IMU, so the extrinsic is not identity. This is untestable
without hardware — it needs the single-axis rotation test on a real device.
It remains the most likely source of a self-consistent, wrong trajectory.

**§4.7 Double compression.** Avoided; see the table above.

**§5 Blur threshold.** `ingest.py` uses 0.35 × the *session* median, which a
live session does not have. Replaced with a running median over the recent
past — also more honest for a real flight where illumination changes. The
flagged fraction is reported and warns above 30 %, because that threshold
was tuned on synthetic renders and could quietly gut the dense stage on
real footage.

**§5 IMU plausibility.** Samples beyond 8 g or 35 rad/s are rejected and
counted. On the synthetic dataset this catches exactly one sample — the
known >100 g one INTEGRATION.md flags.

---

## What initialisation costs, and why

Yaw is the one attitude component neither the accelerometer nor a GPS
position fix can observe, so the filter cannot start until the platform has
moved. Everything arriving before then is buffered and replayed, not
discarded.

The first implementation used `course_over_ground`, which takes a two-point
difference: with metre-scale noise over a short baseline the heading is
mostly noise (36° out after 6 fixes where truth is ~0°). Switching to
`velocity_from_fixes` — a least-squares fit over the same fixes, still in
`georeference.py` — gives a materially better initial heading estimate for
the same reason its docstring gives now: averaging several fixes beats a
two-point difference against metre-scale GPS noise.

Worth knowing (measured under the removed EKF architecture - see
CHANGELOG.md): the initial heading was still ~33° out, and the EKF recovered
it to **0.6°** during flight, because GPS position corrections propagate
into attitude through the filter's cross-covariance terms over a
maneuvering trajectory. **This recovery mechanism no longer exists** - the
current architecture sets yaw once at init (`so3.level_and_align_attitude`)
and never revisits it directly; whatever correction happens after that comes
only from `solvePnPRansac` replacing `pose_R` each frame it succeeds, which
has no absolute yaw reference of its own beyond what init and BA's priors
preserve. Whether that recovers a bad initial heading as well as the EKF did
is unmeasured. ATE alone would not have shown the EKF-era recovery either
way — Umeyama alignment absorbs a constant rotation — which is why the
(now-removed) scoring tool also reported an unaligned georeferenced RMSE
alongside ATE. A model can score a clean ATE and still be 30° out in the
world.

---

## Throughput: the actual real-time gap

Measured on this machine at batch-equivalent quality (`--stereo-stride 2
--keyframe-stride 8`), overall 0.22× real time:

```
per frame, serial (reader thread):  decode 2 ms + blur 5 ms + track 41 ms + BA 88 ms = 136 ms
budget at 30 fps:                   33 ms          -> 4.1x too slow
dense lane:                         138.7 CPU-s per 20 s of capture -> ~6.9 cores
```

Two things follow, and neither is a redesign:

1. **Bundle adjustment is the single largest per-frame cost** — 88 ms,
   more than tracking. It runs inline on the reader thread. Moving it to
   its own thread drops the serial cost to ~48 ms/frame, within 1.5× of
   real time. This is the highest-value next change.
2. **The plane sweep is embarrassingly parallel** over pixels × planes ×
   source views — a batched warp plus a box filter. It already scales
   across worker threads; a GPU is the obvious next step.

---

## Not validated

**No part of the Android app has run on a physical phone.** Everything
above exercises the *receiver*, driven over a real socket by a test double
speaking the same wire format. No measured number here — frame rate, IMU
jitter, GPS behaviour, the extrinsic — is evidence about the handset.

Before trusting a real capture: record a stationary session first (cheapest
calibration available, and it validates the whole path), check
`MAX_GPS_REANCHOR_SIGMA_M` against this device's actual reported GPS
accuracy (see README.md "No principled GPS/vision fusion"), calibrate
intrinsics at the streaming resolution, and run the single-axis rotation
test for §4.6.

---

## Layout

| path | what it is |
|---|---|
| `src/rtvio/live_pipeline.py` | the consumer: three lanes, rolling window, exports |
| `src/rtvio/stream/protocol.py` | wire format ↔ typed packets, encode and decode |
| `src/rtvio/stream/clock.py` | the three clock domains → one timeline |
| `src/rtvio/stream/geodesy.py` | lat/lon/alt → local ENU; per-fix GPS sigma |
| `src/rtvio/stream/source.py` | socket source, reorder buffer, subscriber fan-out |
| `src/rtvio/stream/recorder.py` | fixture writer (test harness) |
| `src/rtvio/stream/replay.py` | fixture → packet stream (debugging) |
| `tests/test_stream.py` | 18 acceptance checks from INTEGRATION.md §8 |
