# Architecture redesign: SLAM / VIO / photogrammetry / 3DGS as pipeline stages

**Status:** design doc, not yet implemented. Written 12 September 2026,
formalizing decisions made during the LingBot-Map evaluation spike (full
detail and evidence in `../../rtviomap/session.md`) rather than re-deriving
them. Read that file first if anything here is unclear about *how* a number
was measured — this doc only states the conclusions and what they imply for
the codebase.

---

## 1. The brief, and the one framing decision everything else follows from

SIH PS-17, "Single-Pass Drone Video to Accurate 3D Model Generation System"
(`../../SIH26158.pdf`, pages 37-39). The requirements that actually constrain
this design:

- Input: single-pass drone video (1080p/4K) + GPS + flight metadata
  (mandatory); IMU/intrinsics/RTK optional.
- Output: georeferenced mesh/point cloud, ≤1m spatial accuracy, **<15
  minutes processing for a 10-minute video**.
- Background text explicitly wants "near real-time situational awareness,"
  not only a batch deliverable.
- Evaluation weights: Accuracy 30%, Completeness 20%, Speed 20%,
  Innovation 15%, Scalability 10%, UI 5%.

The professors separately suggested trying SLAM, VIO, photogrammetry, and
3D Gaussian Splatting "together, maybe." Read literally that's four systems
to integrate; read against the brief above, it's four *jobs* that already
exist at different stages of a reconstruction pipeline, and this codebase
already has a live-streaming architecture (`rtvio/`) that is the right shape
for the "near real-time" requirement. **The redesign keeps that live
architecture as the core and adds a batch front door on top of it — it does
not fork into a separate batch pipeline.** `rtvio/src/rtvio/stream/replay.py`
(built for debugging) already does most of the work of "replay a recorded
video+GPS-log at max speed through the same live pipeline"; formalizing that
into a real front door is one of the two concrete workstreams this doc
hands off (§5).

---

## 2. The technique-to-stage mapping

This is the actual answer to "how do these four combine" — not "run all
four everywhere," but each one doing the specific job it's suited to,
decoupled by data flow so a slow stage never blocks a fast one:

| Technique | Job | Where it runs | Consumes | Produces |
|---|---|---|---|---|
| **VIO** | Real-time front-end pose, every frame | Live, on the socket-reader thread | Frame + IMU stream | A pose per frame, immediately — this is the number a live viewer/situational-awareness display shows |
| **SLAM** (the back-end half of it) | Windowed/global bundle adjustment + opportunistic loop closure over keyframes | Decoupled from the live front end — its own thread/lane, or the post-flight pass | VIO's poses + keyframes | *Refined* poses — globally consistent, not just locally smooth |
| **Photogrammetry (MVS)** | Dense multi-view stereo / point cloud | Fed by the refined (SLAM) poses, not live VIO poses | Refined poses + all frames as supervision | The dense cloud → mesh → DSM |
| **Gaussian Splatting** | A second output head, same posed-image set | Post-flight, alongside or after MVS | The same refined poses + frames MVS uses | An alternative/additional renderable output, for Innovation/Visualization scoring |

Two things this table is deliberately doing:

**It explains why "run SLAM and VIO together" isn't a category error.**
They are not two competitors for the same job. VIO is asked for an answer
*now*, cheaply, every frame, and is allowed to be locally smooth but
globally drifty. SLAM is asked for an answer *eventually*, expensively, over
a window or the whole session, and is allowed to be slow because nothing
live is waiting on it. This is exactly the shape `docs/STREAMING.md`'s
existing three-lane table already has (gyro integration / sparse tracking +
windowed BA / dense stereo, at ~1ms / ~0.3s / ~2-5s latency respectively) —
the redesign is extending a pattern already validated in this codebase, not
inventing one.

**It explains why 3DGS is not free photogrammetry-replacement.** It needs
posed images as input the same way MVS does — normally COLMAP's job, which
has no Windows wheel for this project anyway (`rtvio/README.md`) — so here
it's a second consumer of the *same* refined-pose output MVS uses, not a
shortcut around needing poses at all.

---

## 3. Why the original `rtvio/` pipeline underperforms (motivation for the redesign)

Established by reading `INTEGRATION.md`, `CHANGELOG.md`, `live_pipeline.py`,
and two real capture reports — not re-derived here, only summarized. Full
detail in `rtviomap/session.md` §3.

- **15fps measured vs 30fps target** is the phone's WiFi/TCP JPEG link
  (~10.6 Mbps sustained, 200-450 late-dropped events per session), not the
  camera pipeline. Nothing in this redesign changes that constraint; VIO's
  job is to stay locally accurate *despite* jittery, occasionally-dropped
  frame arrival, not to fix the link.
- **Zero GPS fixes in every real capture ever recorded with this app** —
  root cause not yet confirmed on hardware (needs `ACCESS_FINE_LOCATION`,
  location services on, and the app's `outdoorMode` setting true, all
  simultaneously, on a real outdoor test that has never been run — see
  `rtviomap/session.md` §7). This is the single highest-leverage item this
  whole redesign cannot substitute for: without real GPS, nothing here
  produces a georeferenced output, full stop, regardless of which
  SLAM/VIO/MVS stack sits behind it.
- **"Only 4-5 beams of point cloud" instead of continuous coverage** is a
  CPU-throughput ceiling, not a bug: plane-sweep stereo is ~10x slower than
  real time on CPU, so `dense_stereo.py`'s bounded queue sheds keyframes to
  stay live. The fix in this redesign's terms: MVS/3DGS should consume ALL
  frames as supervision once pose isn't CPU-bound-live anymore (i.e., once
  it's reading SLAM's refined poses from a decoupled stage, not gating on
  the live front end's frame rate).
- **The removed IMU dead-reckoning failed because GPS was always zero to
  correct against** — not evidence against IMU use generally. A
  tightly-coupled VIO (IMU preintegration + vision jointly optimized every
  frame) is a different mechanism and stays locally metric-scaled and
  consistent without GPS; GPS only anchors it to absolute world coordinates
  and bounds long-term drift. This is what §4's literature review
  independently confirms is the standard architecture, not a novel claim.
- **Still unresolved:** `INTEGRATION.md` §4.1's clock-domain mismatch (IMU
  on monotonic-since-boot, frames/GPS on wall clock). A tightly-coupled VIO
  is much less forgiving of this than the old snap-to-GPS code was, since
  it fuses IMU and vision every frame rather than only at GPS arrival.

---

## 4. Two candidate front ends, and where each stands

The mapping in §2 is architecture-agnostic — it says *what job* each stage
does, not *which model* does it. Two concrete options were evaluated for
the VIO+SLAM+MVS front end (§2's first three rows); this section states the
current, honest standing of each rather than picking a winner prematurely.

### 4a. Classical tightly-coupled VIO + SLAM + MVS (the conservative option)

Mature, real-time, CPU-only options exist and don't require the GPU/WSL2
dependency chain §4b needed: **ORB-SLAM3** (most accurate in the benchmarks
reviewed), **VINS-Fusion**, **OpenVINS**, **Kimera-VIO** (beats VINS-Fusion
in most tests). Tightly-coupled beats loosely-coupled empirically in these
benchmarks — independently confirming why the old EKF-based approach in
`rtvio/` failed (§3 above). This remains the safe fallback if §4b's
real-resolution accuracy check (below) doesn't pan out, and is not merely a
hedge — it needs no GPU, no 15-minute wait, and no environment as fragile as
the one §4b required to get working.

### 4b. LingBot-Map (the single-model candidate) — evaluated, promising, not yet a green light

`Robbyant/lingbot-map` — a feed-forward "Geometric Context Transformer,"
monocular RGB in, poses + point cloud out, no explicit bundle adjustment.
Same family as MASt3R-SLAM/VGGT-SLAM (mature precedent for this category —
see `rtviomap/session.md` §5) but newer and reportedly ahead of both on
several benchmarks. The appeal: one model plausibly collapsing §2's VIO +
SLAM + MVS rows into a single forward pass.

**Measured, this session, after resolving three separate environment
breakages to get real FlashInfer running under WSL2** (full diagnostic
detail in `rtviomap/session.md` §4 — Ubuntu 26.04's Python/CUDA-toolkit/gcc
version traps, not LingBot-Map's fault):

| | fps | vs. Windows baseline |
|---|---|---|
| Windows, SDPA fallback (no FlashInfer) | 0.26 | baseline |
| WSL2 + FlashInfer, cold JIT cache | 0.55 | 2.1x |
| **WSL2 + FlashInfer, warm cache (steady-state)** | **~7.0-7.1** | **~27x** |

Against PS-17's budget (6000 frames for a 10-minute flight at 10fps
decimation): **857s ≈ 14.3 minutes for the LingBot-Map pass alone**, against
a 15-minute *total* budget that still has to cover GPS-alignment, meshing,
and export afterward. That is close enough to be worth continuing to
evaluate, and nowhere near comfortable enough to plan the submission around.
It is also only ~35% of LingBot-Map's own claimed ~20fps — real speedup,
real remaining gap.

**The open question is no longer throughput — it's accuracy at 518×294, and
that resolution is not negotiable.** Every number above was measured at
518×294 (their demo's default), not the 1080p/4K PS-17 actually specifies.
Tried raising `--image_size` to test the cost of more resolution
(`rtviomap/run_benchmark_hires.sh`) and found there's no cheap version of
that experiment: the checkpoint's positional embedding is a fixed absolute
embedding sized for exactly 518÷14 = 37×37 patches and doesn't interpolate
to other sizes, so it fails to load outright at any other `--image_size`.
Real footage must be downsampled to ~518×294 before this checkpoint can see
it at all — the question is narrowly whether that preserves enough detail
for ≤1m accuracy, and answering it needs real flight footage plus ground
truth to score against, neither of which currently exists for any real
capture (`docs/STREAMING.md`'s note that ground-truth tooling was removed
along with the synthetic dataset that had it). This is the actual next gate
before committing to this path over §4a, and it is now a data-acquisition
problem more than an engineering one.

---

## 5. Integration architecture (how this actually lands in the codebase)

1. **Keep the live-streaming architecture as the core.** No batch fork.
   `rtvio/src/rtvio/stream/replay.py` is most of the way to being the batch
   front door already — it replays a recorded packet stream through the
   same live consumer machinery used for a real phone connection.
2. **The adapter module** (`rtviomap/adapter.py`, scaffolded — see that
   file's own status note for how complete it is) is a `StreamSession`
   subscriber, same shape as `live_pipeline.py`'s `LiveReconstructor`:
   record every frame + GPS fix as they arrive; drive a decimated subset
   through whichever front end (§4a or §4b) is live for a coarse
   live/situational-awareness preview; once the session ends, run the full
   recorded sequence through that front end's proper batch/windowed
   inference for the real reconstruction.
3. **`rtviomap/align.py`** (written, self-tested — `rtviomap/session.md` §6)
   closes the gap between a monocular model's arbitrary scale/frame (true of
   §4b, and of `rtvio/`'s own existing vision-only pose) and the local-ENU-
   metres frame the rest of the pipeline expects: a closed-form similarity
   transform (Umeyama/Horn — scale + rotation + translation) fit from
   GPS-matched pairs, applied to every point/pose, not just the matched
   ones.
4. **Meshing and export are unchanged.** `rtvio.meshing.build_grid`/
   `write_textured_mesh` and `rtvio.export.*` already operate purely on
   local-ENU-metres points + a reference lat/lon/alt origin — once §5.3
   lands a front end's output in that frame, these modules need zero
   modification regardless of which front end (§4a or §4b) produced the
   points.
5. **3DGS is a second, optional output head** off the same posed-image set
   §5.2/§5.3 already assembles for MVS — not a separate pipeline requiring
   its own pose estimation.

---

## 6. Open items, in priority order

1. **The real outdoor GPS-lock test.** Independent of every decision in this
   doc — without it, nothing downstream is georeferenced no matter which
   front end wins. Confirm `ACCESS_FINE_LOCATION` granted, location services
   on, the app's `outdoorMode` toggle true, and patience for a genuine cold
   satellite fix, on a real device outdoors. This can only be done by a
   person with the hardware; it has been the single highest-leverage
   unresolved action for multiple sessions running.
2. **§4b's 518×294 accuracy check against real footage + ground truth** —
   the actual gate on whether LingBot-Map replaces §4a's classical stack,
   now that throughput is no longer the open question and higher resolution
   isn't available to try instead (§4b).
3. **Finish the adapter module** (`rtviomap/adapter.py` — scaffolded and
   self-test-verified, model calls still a seam per its own module
   docstring) end-to-end against whichever front end §6.2 favors.
4. **`INTEGRATION.md` §4.1's clock-domain mismatch** — unresolved, and more
   consequential for a tightly-coupled VIO (§4a) than it was for the old
   snap-to-GPS architecture.
