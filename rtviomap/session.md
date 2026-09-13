# RTVIO pipeline redesign — session handoff

**Audience:** the next Claude Code session (or the user) picking this up.
**Written:** 12 September 2026, at the point the user rebooted for WSL2 setup.
**Updated:** same day, later — the user asked to work autonomously for a few
hours after the reboot resolved. See the "Done, this session" note at the
top of §7 and the new §4 subsection for what changed: the WSL2/LingBot-Map
throughput question is **resolved** (real number in hand, verdict is "close
but not comfortable," not a clear win), the architecture doc from the old
§7 item 5 is **written** (`rtvio/docs/ARCHITECTURE_REDESIGN.md`), and the
adapter module is **scaffolded and self-test-verified**
(`rtviomap/adapter.py`) though not yet wired to a real model. The GPS-lock
test remains the one item nobody but the user, on the physical device, can
do.

Read this top to bottom before doing anything. It exists so the next session
does not re-derive facts that took real tool calls (log reading, code
reading, benchmarking, web research) to establish the first time.

---

## 1. Why this exists at all

The user's team has a hackathon problem statement — **SIH PS-17, "Single-Pass
Drone Video to Accurate 3D Model Generation System"** (full text in
`C:\Users\HP\Desktop\RTVIO\SIH26158.pdf`, pages 37-39). Key requirements
worth not re-deriving:

- Input: single-pass drone video (1080p/4K) + GPS + flight metadata
  (mandatory); IMU/intrinsics/RTK optional.
- Output: georeferenced 3D mesh/point cloud — OBJ/PLY/LAS/GeoTIFF/glb/gltf/fbx.
- **Spatial accuracy ≤ 1 m. Processing time < 15 minutes for a 10-minute
  video.** (This second number is the one that keeps mattering below.)
- Background text explicitly wants "near real-time situational awareness,"
  not just a batch deliverable — this matters because the existing
  `rtvio/` pipeline is architected as a **live** system, and that turned out
  to be defensible against the brief, not a mismatch to fix.
- Evaluation weights: Accuracy 30%, Completeness 20%, Speed 20%,
  Innovation 15%, Scalability 10%, UI 5%.

The user's professors separately suggested trying **SLAM, VIO, photogrammetry,
and Gaussian Splatting "together, maybe"** as the redesign direction. Most of
this session was spent (a) diagnosing why the *existing* `rtvio/` pipeline
underperforms, (b) mapping those four techniques onto concrete pipeline
stages rather than treating them as four things to bolt on, and (c)
evaluating one specific candidate model (LingBot-Map) as a possible
replacement for two of those stages at once.

---

## 2. Decisions already made (don't re-litigate these)

1. **Keep the live-streaming architecture as the core**, and add a thin
   "replay a recorded video+GPS-log at max speed through the same live
   pipeline" front door on top — not a batch fork. `rtvio/src/rtvio/stream/replay.py`
   already exists for this (built for debugging) and is most of the way there.
2. **Technique mapping** (this is the actual answer to "how do SLAM/VIO/
   photogrammetry/3DGS combine," not "run all four"):
   - **VIO** = real-time front-end pose, every frame.
   - **SLAM** = the back-end: windowed/global bundle adjustment + (opportunistic)
     loop closure over keyframes, decoupled from the live front end.
   - **Photogrammetry (MVS)** = dense multi-view stereo fed by the *refined*
     (SLAM) poses, not live VIO poses.
   - **Gaussian Splatting** = a second output head off the same posed-image
     set, for the Innovation/Visualization rubric lines — plays the role
     COLMAP would normally play for 3DGS init, except our own VIO/SLAM+MVS
     output replaces COLMAP (which has no Windows wheel anyway, see
     `rtvio/README.md`).
3. **The professors' four techniques are jobs, not a checklist** — see the
   table in this session's chat log (or re-derive: VIO=front-end pose,
   SLAM=back-end consistency, MVS=dense mesh, 3DGS=innovation/viz head).
4. **LingBot-Map is being evaluated as a candidate to replace the VIO+SLAM+MVS
   front end in one model**, not as a guaranteed adoption — see §4.

---

## 3. Root causes established for the ORIGINAL rtvio pipeline's problems

These came from reading `INTEGRATION.md`, `CHANGELOG.md`, `live_pipeline.py`,
and two real capture reports (`rtvio/data/outputs/output_flight1/REPORT.md`,
`output_my_flight/REPORT.md`). Don't re-derive:

- **15fps measured vs 30fps target**: it's the phone's WiFi/TCP JPEG link
  (194MB in ~146s ≈ 10.6 Mbps sustained, with 200-450 "late-dropped" events
  per session), **not the camera pipeline** — `CameraCapture.kt` correctly
  requests 30fps from the sensor.
- **Zero GPS fixes, in every real capture ever recorded with this app**
  (`CHANGELOG.md` says this outright). Root cause not yet confirmed on
  hardware — `GpsCollector.kt` deliberately uses raw `GPS_PROVIDER` (correct
  design choice, not a bug) which needs genuine sky visibility, granted
  `ACCESS_FINE_LOCATION`, location services on, and the app's `outdoorMode`
  setting true (default true). **Nobody has yet confirmed all four were true
  during a real test.** This is still the single highest-leverage unresolved
  action item from earlier in this conversation.
- **"Only 4-5 beams of point cloud" instead of continuous coverage**: not a
  bug, a CPU-throughput ceiling. `dense_stereo.py` only considers every 8th
  frame (`KEYFRAME_STRIDE=8`) and requires a 4-20m baseline (real geometry,
  not arbitrary) — of those candidates, most got **shed** because plane-sweep
  stereo is ~10x slower than real time on CPU and `DENSE_QUEUE_DEPTH=4`
  forces dropping keyframes to stay live. Measured: 10/11 keyframes survived
  out of ~35/19 candidates in the two real sessions, at 2-7% mesh
  completeness. The fix isn't retuning the 4-20m constant in isolation — it's
  feeding a real SLAM's poses into full multi-view fusion (or 3DGS) that
  uses ALL frames as supervision, once pose isn't CPU-bound-live anymore.
- **"IMU-driven trajectory didn't work before, why try again?"** — what was
  removed (`inertial_nav_ekf.py`) was **loosely-coupled IMU dead-reckoning**
  with occasional external correction. It failed because GPS was always
  zero, so it had nothing to correct against and drifted by construction —
  not evidence against IMU use in general. A **tightly-coupled VIO** (IMU
  preintegration + vision jointly optimized every frame, MSCKF/VINS/
  OKVIS-style) is a different thing and doesn't need GPS to stay locally
  metric-scaled and consistent; GPS only anchors it to absolute world
  coordinates and bounds long-term drift. This is exactly what the
  literature review below independently confirms.
- Still unresolved: `INTEGRATION.md` section 4.1's clock-domain mismatch
  (IMU on monotonic-since-boot, frames/GPS on wall clock) — a tightly-coupled
  VIO is much less forgiving of this than the old snap-to-GPS code was.

---

## 4. LingBot-Map evaluation — where it stands right now

`Robbyant/lingbot-map` (ECCV 2026 oral, arXiv 2604.14141) — a feed-forward
"Geometric Context Transformer," monocular RGB in, poses + point cloud out,
no explicit bundle adjustment. Same family as MASt3R-SLAM/VGGT-SLAM (see §5)
but newer, and reportedly ahead of both on several benchmarks.

**What's built and verified so far, all under `C:\Users\HP\Desktop\RTVIO\rtviomap\`:**

| Path | What it is |
|---|---|
| `rtviomap/lingbot-map/` | Cloned repo. One local patch applied: `lingbot_map/vis/point_cloud_viewer.py`'s `cm.get_cmap('viridis')` → `cm.colormaps['viridis']` (matplotlib >=3.9 removed the old call; their pin predates that removal). |
| `rtviomap/.venv/` | Python 3.13 venv, `--system-site-packages` (inherits the machine's existing `torch 2.12.0.dev20260408+cu128`, CUDA confirmed available — RTX 5070 Ti, 16GB VRAM). `pip install -e "./lingbot-map[vis]"` done. **FlashInfer install FAILED here** (native Windows) — its build needs `apache-tvm-ffi==0.1.0b15`, unavailable for this platform. This is the whole reason WSL2 is being set up: FlashInfer's prebuilt wheels target Linux. |
| `rtviomap/checkpoints/lingbot-map.pt` | Downloaded, 4.63GB, from `huggingface.co/robbyant/lingbot-map`. Don't re-download — WSL setup should reuse this file via `/mnt/c/...` (see `wsl_setup.sh`). |
| `rtviomap/wsl_setup.sh` | **Run successfully, end to end** — three real bugs found and fixed along the way (Python 3.14/no-3.10 on this Ubuntu release, missing CUDA Toolkit, gcc-15/nvcc incompatibility — full detail below). Clones a second copy of lingbot-map onto WSL's native ext4 (git/pip are slow over `/mnt/c`), gets a working Python 3.12 via `uv` (not apt), installs `torch==2.8.0+cu128` + CUDA Toolkit 12.9 + gcc-14 + real FlashInfer, and runs the same courthouse smoke test. Reproducible from scratch: `wsl -d Ubuntu -u root -- bash /mnt/c/Users/HP/Desktop/RTVIO/rtviomap/wsl_setup.sh`. |
| `rtviomap/run_benchmark.sh` | Reruns just the courthouse benchmark against the already-set-up WSL environment (sanity checks + timing), without repeating the full setup. Use this, not `wsl_setup.sh`, for a quick re-check. |
| `rtviomap/align.py` | GPS-anchoring utility, **written and self-tested, working**. See §6. |
| `rtviomap/adapter.py` | `StreamSession` subscribers wiring a front end into rtvio's pipeline, **scaffolded and self-test-verified** (glue code proven, model calls still a documented seam). See §7 item 3. |
| `rtvio/docs/ARCHITECTURE_REDESIGN.md` | The technique-mapping doc from decision #2 below, **written**. Formalizes VIO/SLAM/MVS/3DGS as pipeline stages, states the §4 LingBot-Map verdict, and lays out the integration plan `adapter.py` implements. |

**Measured performance (native Windows, SDPA fallback, no FlashInfer):**
ran `demo.py --model_path checkpoints/lingbot-map.pt --image_folder example/courthouse --use_sdpa`
(286 frames, 518×294). **1083.4 seconds total → ~0.26 fps**, against their
claimed ~20fps — **~77x slower**. Reconstruction itself was correct (no
errors in the model/inference path, only a viz-layer matplotlib crash
afterward, now fixed). GPU memory: 13.17GB allocated peak (sane, within the
16GB card), but reported "reserved peak 66.15GB" — turned out to be exactly
what was suspected: a reporting artifact of `expandable_segments not
supported on this platform` (Windows-only limitation of the CUDA allocator).
**Root-caused, resolved** — see the WSL numbers below, where the same field
reads a sane 14.40GB.

### WSL2 + FlashInfer: done, root-caused three separate breakages, final numbers in

The WSL2 detour is **complete**. Getting from "WSL2 installed" to "a correct,
reproducible warm-cache number" required diagnosing and fixing three
unrelated environment problems, each hit only once real hardware/software
combinations were tried (none of this was foreseeable from the README):

1. **Ubuntu 26.04 "Resolute"** (what `wsl --install -d Ubuntu` actually
   installs as of this writing) ships Python 3.14 by default and dropped
   3.10 (the script's original pin) from its repos entirely — and torch
   2.8.0+cu128 has no 3.14 wheel. Fixed by switching `wsl_setup.sh` to `uv`
   (astral.sh) for a standalone Python 3.12, independent of whatever apt
   carries on a given Ubuntu release.
2. **No CUDA Toolkit (`nvcc`) was installed at all** — torch's pip wheel
   bundles only runtime `.so` files, not the compiler, and FlashInfer must
   JIT-compile its attention kernels on first use since no prebuilt
   `flashinfer-jit-cache` build exists yet for `flashinfer-python==0.6.18.post1`
   (confirmed by checking the wheel index directly — latest available cache
   build is 0.6.17). Fixed by installing `cuda-toolkit-12-9` via NVIDIA's
   `wsl-ubuntu` apt repo (a distro-agnostic bucket, not tied to the 26.04
   codename, so this one doesn't hit the same trap as #1).
3. **nvcc 12.9 refuses to compile with a host GCC newer than 14** ("gcc
   versions later than 14 are not supported"), but Ubuntu 26.04 ships gcc-15
   by default. Fixed by installing `gcc-14`/`g++-14` from universe and
   pinning nvcc to it via `NVCC_PREPEND_FLAGS="-ccbin g++-14"` (nvcc's own
   supported mechanism — every downstream nvcc invocation, including
   FlashInfer's runtime JIT builds, picks it up automatically).

All three fixes are patched into `rtviomap/wsl_setup.sh` itself, not just
worked around live, so a from-scratch run reproduces this environment
without re-deriving any of the above. (A fourth, much smaller bug: the
matplotlib patch from the Windows clone, `cm.get_cmap` → `cm.colormaps`,
doesn't actually work either on matplotlib 3.11 — the registry lives on the
top-level `matplotlib` module, not the `cm` submodule. Fixed to
`matplotlib.colormaps['viridis']` plus the missing `import matplotlib`, and
patched into `wsl_setup.sh`'s clone step too.)

**Final measured numbers**, same courthouse benchmark (286 frames, 518×294),
via `rtviomap/run_benchmark.sh`:

| Run | Total time | Overall fps | Notes |
|---|---|---|---|
| Windows, SDPA (no FlashInfer) | 1083.4 s | 0.26 fps | baseline |
| WSL2, cold FlashInfer cache | 517.6 s | 0.55 fps | pays every kernel's one-time JIT-compile cost inline |
| WSL2, warm FlashInfer cache (×2, reproduced) | 39.5 s / 39.9 s | ~7.2 fps avg | **the real number** |

The warm run's per-frame rate (from its own progress bar, frames 30–286,
essentially flat) settles at **~7.0–7.1 fps steady-state** — this, not the
39.9s/286 average (which still includes a few warmup frames), is the right
number to use for any budget projection. GPU memory during the warm run:
13.74GB allocated / **14.40GB reserved** — confirms the 66GB Windows figure
above was purely a Windows allocator-reporting artifact, not a real leak.

**Verdict against PS-17's budget:** using session.md's own 6000-frame
assumption (10-minute flight, 10fps decimation) —

- at 7.0fps steady-state: 6000 / 7.0 = **857s ≈ 14.3 minutes** for the
  LingBot-Map pass alone, against a 15-minute *total* budget. That leaves
  well under a minute for `align.py`, meshing, export, and everything else
  the pipeline still has to do afterward. **This does not comfortably fit —
  it is close enough to be worth pursuing further, not close enough to call
  solved.**
- This is a genuine, reproducible **~27x speedup** over the Windows SDPA
  baseline (1083.4s → 39.9s) and validates that the WSL2 detour was worth
  doing. It is also only **~35% of their claimed ~20fps** — real, but a real
  gap remains, and nobody should plan around hitting 20fps on this hardware.
- **Important caveat neither number above accounts for:** this was measured
  at 518×294 (their demo's downsampled resolution), not the 1080p/4K PS-17
  actually specifies. Whether decimating/downsampling real flight footage to
  a similar scale before feeding LingBot-Map preserves enough detail for the
  ≤1m accuracy requirement is untested and is now the actual open question,
  not raw throughput.
- **`--image_size` is not a free dial — confirmed by trying.** Ran
  `rtviomap/run_benchmark_hires.sh 1036` (2x default) expecting at worst a
  slower run; instead the checkpoint failed to load at all:
  `size mismatch for aggregator.patch_embed.pos_embed: copying a param with
  shape [1, 1370, 1024] from checkpoint, the shape in current model is
  [1, 5477, 1024]`. 1370 = 37×37 patches (518÷14) + 1 CLS token — this is a
  **fixed absolute positional embedding**, not one that interpolates to
  other input sizes (unlike e.g. DINO's `interpolate_pos_encoding`; this
  codebase doesn't implement that). This sharpens, rather than just flags,
  the caveat above: there is no cheap "just run it bigger" experiment
  available — real footage genuinely has to be downsampled to ~518×294
  before this checkpoint can see it at all, so the accuracy question is
  squarely "is 518×294 enough," not "how much resolution can we afford."

**Practical fallout:** the live/post-flight frame-count split from the
original "50% post-streaming is fine" plan needs revisiting with this
tighter number in hand rather than assumed comfortable, and the classical
tightly-coupled VIO + SLAM + MVS fallback (§5 table — ORB-SLAM3, VINS-Fusion,
Kimera-VIO, none of which need a GPU or a 15-minute wait) stays a live
option, not just a hedge, until real-resolution accuracy is checked.

**API surface confirmed by reading `demo.py` and `lingbot_map/models/gct_stream.py`
directly (not just the README) — this is the integration contract:**

- `GCTStream(img_size=, patch_size=, enable_3d_rope=, max_frame_num=, kv_cache_sliding_window=, kv_cache_scale_frames=, kv_cache_cross_frame_special=True, kv_cache_include_scale_frames=True, use_sdpa=, camera_num_iterations=)`
  then `.load_state_dict(ckpt.get("model", ckpt), strict=False)`, `.to(device).eval()`.
- **For a live/decimated path** (frames arriving one at a time from the phone,
  total count unknown in advance): drive `model.forward(frame_tensor, num_frame_for_scale=scale_frames, num_frame_per_block=1, causal_inference=True)` directly, per frame, under `torch.no_grad()` +
  `torch.amp.autocast("cuda", dtype=dtype)`. Needs an initial buffer of
  `num_scale_frames` (default 8) frames processed together first (Phase 1,
  bidirectional scale-anchoring) before per-frame causal streaming (Phase 2)
  can start — directly analogous to `live_pipeline.py`'s existing
  "buffer until yaw/course is observable, then start" pattern. Use
  `model.clean_kv_cache()` to reset between sequences, `model._set_skip_append(bool)`
  for keyframe-interval skipping.
- **For the post-flight full pass** (complete recorded sequence, count known):
  just call `model.inference_streaming(images, num_scale_frames=, keyframe_interval=)`
  (handles Phase 1 + the per-frame loop internally, simpler, well-tested by
  their own demo) or `model.inference_windowed(...)` for sequences >3000
  frames (their own documented threshold before quality degrades without it).

This two-call split maps directly onto the user's accepted hybrid plan: live
`forward()` calls for a coarse live preview, `inference_streaming()`/
`inference_windowed()` for the real, full-quality post-flight reconstruction.

**Also confirmed:** `import rtvio` already works from `rtviomap/.venv`
(rtvio's `pip install -e .` was done into the same system Python this venv
inherits via `--system-site-packages`) — so the adapter can call
`rtvio.stream.geodesy.latlon_to_enu`, `rtvio.georeference.*`,
`rtvio.meshing.build_grid`/`write_textured_mesh`, `rtvio.export.*` with
**zero modification**, once LingBot-Map's output is aligned into the same
local-ENU-metres frame those functions already expect.

---

## 5. Research landscape (don't re-search this from scratch)

Full findings and sources are in the chat log two exchanges before this one.
Summary table:

| Category | Examples | Relevance |
|---|---|---|
| Classical tightly-coupled VIO (mature, real-time, no GPU needed) | ORB-SLAM3, VINS-Fusion, OpenVINS, Kimera-VIO | Benchmark: tightly-coupled beats loosely-coupled empirically (exactly why the old EKF failed); ORB-SLAM3 most accurate; Kimera beats VINS-Fusion in most tests. Realistic fallback/companion to LingBot-Map if it doesn't pan out. |
| Feed-forward dense SLAM (learned, no explicit BA) | DROID-SLAM, MASt3R-SLAM (15fps, CVPR25), VGGT-SLAM/2.0 | Same family as LingBot-Map, older/more battle-tested. |
| VIO/SLAM + 3D Gaussian Splatting fusion | Photo-SLAM, SplaTAM, MonoGS, GS-SLAM (all CVPR24), **Splat-SLAM** (RGB-only monocular, closest match to our sensor), RTG-SLAM (SIGGRAPH24, RGB-D not monocular) | Confirms "VIO+SLAM+3DGS together" is a live ~2yr-old research category, not a novel ask. Tracking repo: `github.com/3D-Vision-World/awesome-NeRF-and-3DGS-SLAM`. |
| UAV-specific real-time | "Real-time dense 3D reconstruction from monocular video by low-cost UAVs" (2021, arXiv 2104.10515) — SLAM→MVS→surfel-fusion, 30fps at 768×448 | Closest architectural template to our exact use case. |
| Consumer apps | Polycam, Scaniverse (LiDAR-based, sidesteps VIO entirely), Luma AI (monocular→3DGS, but heavy lifting is cloud-side, not truly on-device real-time) | Context for what "real-time enough" means in shipping products. |
| `ch1bo/drone-reconstruction` (GitHub) | COLMAP→Nerfstudio splatfacto, offline, 30-60min/RTX4070Ti | Independently hit the SAME walls we did: "monocular needs GPS for scale," "~5m consumer GPS accuracy limits geo-accuracy," thin-overlap/repetitive-texture flight patterns break it. Confirms these are known hard constraints of this exact problem, not our bugs. |

---

## 6. `align.py` — what it does, why, confirmed working

LingBot-Map (like any monocular feed-forward model) has **no metric scale
and no fixed world frame** — same ambiguity rtvio's own vision-only pose
already has. A per-frame GPS snap doesn't fix this (that corrects a
trajectory already in the right units; this one isn't yet). The fix is a
**closed-form similarity transform** (Umeyama/Horn: scale + rotation +
translation) fit between the model's raw trajectory and GPS ENU fixes
(matched by timestamp, with a `max_time_diff_s` gate so a stale pairing can't
poison the fit), applied to every point/pose — not just the GPS-matched
ones — landing everything in the same local-ENU-metres frame rtvio's
existing meshing/export code already expects.

`python align.py` self-test passes: exact recovery in the noise-free case;
realistic ~2m-noise/1Hz/jittered GPS recovers the transform to error
commensurate with that noise. (One test threshold of mine was initially too
tight — 1.0m against 2m-noise input — fixed to a properly-justified 2.5m
bound; the alignment math itself was correct on the first attempt.)

**Not yet done:** wiring this into an actual live adapter that consumes
rtvio's phone-stream subscriber pattern and feeds both LingBot-Map's
per-frame `forward()` and the post-flight `inference_streaming()`. This was
the explicit next step being discussed when the session paused for the
reboot.

---

## 7. Immediate next steps, in order

**Done, this session (12 Sept, after the reboot):**

1. ~~Run `rtviomap/wsl_setup.sh` inside WSL2~~ — done, three real bugs found
   and fixed (Python 3.14/no-3.10, missing CUDA Toolkit, gcc-15/nvcc
   incompatibility — full detail in §4). Final warm-cache number: **~7.0fps
   steady-state, ~27x faster than the Windows baseline, but only ~35% of
   their claimed 20fps and a tight (not comfortable) fit against PS-17's
   15-minute budget** — see §4's verdict, including the untested-at-full-
   resolution caveat.
2. ~~Root-cause the 66GB "reserved" memory figure~~ — confirmed Windows-only
   allocator-reporting artifact; WSL reports a sane 14.40GB reserved.

**Still open, roughly in priority order:**

1. **Check accuracy at 518×294 against real footage.** Tried the cheap
   version of this first (`run_benchmark_hires.sh` — just run the existing
   checkpoint at a larger `--image_size`) and it's not available: the
   checkpoint's positional embedding is fixed at 518×518's patch count and
   doesn't interpolate (§4's new bullet has the exact error). So real
   footage genuinely must be downsampled to ~518×294 before this checkpoint
   can process it at all — the open question is narrowly "does 518×294
   preserve enough detail for ≤1m accuracy," and answering it needs (a) real
   flight footage, and (b) some ground truth to score against, which
   `rtvio/docs/STREAMING.md` notes doesn't currently exist for any real
   capture (the synthetic dataset that did was removed). This is now a data
   problem, not a code problem.
2. **Decide the live/post-flight frame-count split** against the user's
   original "50% post-streaming is fine" tolerance, using the *tightened*
   budget math in §4 (857s of a 900s budget for LingBot-Map alone) rather
   than the comfortable assumption that motivated that tolerance originally.
3. **Write the adapter module** in `rtviomap/` — **scaffolded this session,
   see `rtviomap/adapter.py`.** Two `StreamSession` subscribers:
   `LiveMapPreview` (live, optional, decimated per-frame calls for a coarse
   preview) and `BatchMapReconstructor` (driven by replaying the
   `SessionRecorder` fixture through `stream/replay.py`'s
   `ReplayPacketSource` — the actual "replay front door" from decision #1
   above, not a separate batch pipeline). **Verified working end-to-end**
   via `adapter.py`'s own `_selftest()` (`python adapter.py` from inside
   `rtviomap/`, passes): a synthetic front end's arbitrary-frame trajectory
   correctly round-trips through `align.py`'s Umeyama fit and
   `rtvio.meshing`/`rtvio.export` with zero modification to those modules —
   this is the whole integration contract from `ARCHITECTURE_REDESIGN.md`
   sec 5, proven, independent of which front end (LingBot-Map or the
   classical fallback) ends up behind it. **Not done:** the actual
   `model_forward`/`model_batch_infer` callables are still a documented seam
   — real JPEG decode and the real LingBot-Map call. They can't be wired
   from rtvio's native-Windows venv (no `lingbot_map` import there — see §4);
   next session should decide between running this adapter inside WSL2
   directly, or a small WSL-side inference service this file's callables
   talk to (`ARCHITECTURE_REDESIGN.md` sec 6).
4. Two items from earlier in the conversation remain open and independent of
   the LingBot-Map track — don't lose them:
   - The **real outdoor GPS-lock test** (confirm `ACCESS_FINE_LOCATION`
     granted, location services on, app's `outdoorMode` toggle true, patience
     for a genuine cold satellite fix) was repeatedly identified as the
     single highest-leverage unresolved action and has never been executed.
     **Nobody but the user, on the physical device, can do this one.**
   - ~~The Stage 1 architecture doc~~ — written this session, see
     `rtvio/docs/ARCHITECTURE_REDESIGN.md`.
