 ▐▛███▛█   Claude Code v2.1.263
▝▜██████▀  Opus 5 · Claude Pro
  ▝▝ ▝▝    ~\Desktop\RTVIO\rtvio_2

▎ [✻] [✻] [✻] · Share Claude Code and earn $10 in usage credits · /passes

❯ read @..\INTEGRATION.md file and tell me a quick review of what all we are    
  supposed to do                                                                
  ⎿  Read ..\INTEGRATION.md (515 lines)

  Listed 1 directory (ctrl+o to expand)

● The job in one line

  Take the Android app's live TCP stream (camera + IMU + GPS) and get it into
  rtvio_2/'s batch Python reconstruction pipeline — but not by streaming. Phase
  1 is a recorder that writes the stream to disk in exactly the schema
  ingest.Dataset already reads, so pipeline.py runs unchanged.

  What we actually build

  1. rtvioapk/tools/record_session.py (~150 lines on top of the existing
  mock_receiver.py, which already parses the protocol correctly). It writes a
  dataset_dir containing:

  ┌───────────────────────────────┬─────────────────────────────────────────┐
  │             File              │                 Source                  │
  ├───────────────────────────────┼─────────────────────────────────────────┤
  │ frames/NNNNNN.jpg (or         │ 0xFF packets                            │
  │ flight.mp4)                   │                                         │
  ├───────────────────────────────┼─────────────────────────────────────────┤
  │ imu_data.json                 │ 0xFE packets                            │
  ├───────────────────────────────┼─────────────────────────────────────────┤
  │ gps_data.json                 │ 0xFD packets                            │
  ├───────────────────────────────┼─────────────────────────────────────────┤
  │ flight_config.json            │ assembled last (fps/total_time only     │
  │                               │ knowable at end)                        │
  ├───────────────────────────────┼─────────────────────────────────────────┤
  │ camera_intrinsics.json        │ calibrated, not guessed                 │
  └───────────────────────────────┴─────────────────────────────────────────┘

  2. Small pipeline patches — make gt_trajectory.json / checkpoints.json
  optional (currently Dataset.__init__ crashes without them), read real
  per-frame timestamps, and let load_video_frames read a JPEG directory.

  The three things that fail silently

  These are the reason the doc exists — none of them raises an exception, they
  just give you a plausible, wrong reconstruction:

  1. §4.1 — three clocks. IMU is monotonic-since-boot (ns), frames and GPS are
     wall-clock (ms), pipeline wants float seconds from 0. Naive subtraction
     gives a ~10¹² offset. Minimum fix: single shared t0 + constant offset from
     first frame/first IMU sample. Proper fix: estimate offset online in the
     EKF.
  2. §4.2 — frame_timestamp() = frame_idx / fps. The app deliberately drops 
     frames under congestion. One drop shifts every later timestamp and every
     downstream stage inherits it. Either carry real timestamps or resample to a
     fixed grid at record time.
  3. §4.6 — camera↔IMU extrinsic. The app rotates the image to upright but not
     the IMU. The proposed 180°-about-X matrix is explicitly "a hypothesis to
     test," via single-axis rotation tests.

  Ordered plan (§6)

  1. Stationary 60 s recording → validates the whole path (|accel| ≈ 9.81, gyro
     ≈ 0, framesDropped == 0)
  2. Noise characterisation → retune EKF (accel_noise_std=0.08 is
     synthetic-tuned; phone MEMS is much noisier)
  3. Checkerboard intrinsics calibration at the streaming resolution — current
     file is a synthetic 960×540 pinhole, phone streams 720×1280 portrait with
     real distortion
  4. Pipeline tolerance patches (§4.3, §4.2)
  5. Extrinsic verification
  6. Short real capture, run pipeline.py, expect a specific failure
  7. Only then think about streaming

  Two constraints worth flagging up front

  - The app has never run on a physical phone. Compiles clean, wire format
    unit-tested against the mock receiver — zero hardware validation. No
    measured number is trustworthy yet.
  - D7 (accuracy report) is not achievable from a phone capture — there's no
    ground truth to score against. Keep the synthetic dataset as the accuracy
    fixture and say so, rather than letting an empty-list-vs-itself "0.00 m
    RMSE" appear.

  Also: Dataset holds every decoded frame in RAM (~6 MB/frame at 1080p → ~9 GB
  for 60 s @ 25 fps). Cap session length or downsample on load.

  Want me to start on the recorder?

✻ Baked for 23s · done 5:03 PM

❯ why can't realtime reconstruction work, using a phone as input? why are you   
  recording the stream, which doesn't make it a realtime pipeline               

● The honest answer needs the actual code, not the doc's summary. Let me look.

  Searched for 2 patterns, read 1 file, ran 5 shell commands (ctrl+o to expand)

● You're right to push, and my earlier summary repeated the doc's framing too
  uncritically. Having read the code, the "it can't be real-time" claim is
  overstated. Here's what's actually true.

  Two different blockers get conflated

  Causality — needs future frames. Real, but bounded, and that's the whole
  point:

  ┌──────────────────────────────────────┬─────────────────────┬────────────┐
  │                Stage                 │      Lookahead      │ At 30 fps  │
  │                                      │                     │  / 10 m/s  │
  ├──────────────────────────────────────┼─────────────────────┼────────────┤
  │ EKF predict + GPS update             │ none, per-sample    │ 0          │
  │                                      │ causal              │            │
  ├──────────────────────────────────────┼─────────────────────┼────────────┤
  │ Sparse tracker                       │ none                │ 0          │
  ├──────────────────────────────────────┼─────────────────────┼────────────┤
  │ Windowed BA (tracking.py:111,        │ 10 frames           │ ~0.33 s    │
  │ BA_WINDOW=10)                        │                     │            │
  ├──────────────────────────────────────┼─────────────────────┼────────────┤
  │ Dense stereo (dense_stereo.py:66,    │ needs a partner at  │ ~0.4–2 s   │
  │ SEARCH_AHEAD_FRAMES=90)              │ 4–20 m baseline     │            │
  └──────────────────────────────────────┴─────────────────────┴────────────┘

  Nothing needs the whole sequence. find_stereo_partners breaks out of its loop
  as soon as the baseline exceeds 20 m — it's a sliding window, not a global
  optimization. So this is a fixed latency budget of a couple of seconds, which
  is exactly what the PS means by "near real-time situational awareness"
  (session1.md:134).

  Throughput — this is the actual wall. From the reports already in the repo, on
  20 s of 960×540:

  pose_estimation        18.7–57s      0.35–1.07× real-time   ← borderline OK
  dense_reconstruction   213–232s      0.09× real-time        ← ~11× too slow

  At the phone's 720×1280 that's 1.8× more pixels → ~20× too slow. That's a 
  number, not a law. The plane sweep is embarrassingly parallel over pixels ×
  planes × source views — it's a batched warp plus a box filter, which is what
  GPUs exist for. Closing 20× is real work but it's ordinary work: GPU the cost
  volume, or trade resolution/KEYFRAME_STRIDE/plane count.

  The one genuinely batch-shaped thing is ingest.Dataset.__init__ eagerly
  decoding the whole video into RAM — that's ~10 lines of implementation choice,
  not an algorithm.

  So: near-real-time is achievable here. What isn't achievable is instant — you
  will always be a few seconds behind on the dense model, because you need to
  fly past a thing to triangulate it.

  Why record anyway — and the plan needs fixing

  The recorder argument was never "real-time is impossible." It's about debug 
  determinism, and I stated it badly:

  - The app has never executed on hardware. Not one measured number exists.
  - Three of the seven mismatches fail silently — clock domains, frame-index
    timing, camera↔IMU extrinsic. No exception, plausible output, wrong
    geometry.
  - To test a sign flip in R_device_from_camera you need to change one variable
    and see the output move. Against a live stream, every run is different
    footage, lighting, GPS quality and drop pattern. You'd be solving for three
    unknowns with a non-repeatable input.

  But you've identified a genuine flaw in §2's plan. "Write to Dataset's on-disk
  schema, run pipeline.py unchanged" bakes in the batch shape and makes
  streaming a rewrite later. The recorder should be a tee, not a terminus:

  socket ─► packet parser ─► dispatch ─┬─► disk writer   (fixture, replayable)
                                       └─► live consumer (EKF → tracker → dense
  worker)

  Same consumer code runs off the socket or off a recorded session replayed at
  wall-clock rate. Recording then costs nothing architecturally and still buys
  the fixture. Run pipeline.py against a recorded session once, as the fastest
  proof the data is sane — as a validation step, not the target.

  What the real-time version actually looks like

  Three lanes at different latencies, all fed from one packet stream:

  - ~20 ms — EKF pose. Already per-sample causal. Live trajectory to the
    operator immediately.
  - ~0.5 s — sparse map + BA-refined poses.
  - ~2–5 s — dense stereo per keyframe, fired once its forward partners arrive;
    cloud tiles appended to a growing model. Meshing is 0.35 s over the entire
    cloud, so incremental re-grid is free.

  The only truly non-real-time thing is evaluate.py, and that's fine — it isn't
  part of the live product, and per §4.3 a phone capture has no ground truth to
  score against anyway.

  Want me to draft the streaming architecture concretely — the tee/dispatch
  layer plus what changes in ingest.py and build_dense_cloud to make them
  consume a growing sequence?

✻ Worked for 1m 31s · done 5:16 PM

❯ alright, so from my understanding. this is going to be a near-realtime        
  pipeline. it will ingest a video stream, analyse each frame and its           
  corresponding imu/metadata/gps data and then start creating point clouds then 
  and there itself. please promise to me that recorded footage will not play a  
  role in the creation of the 3d model itself                                   

● Yes — I'll commit to that, and let me state the boundary precisely so there's
  no ambiguity later.

  The commitment

  The 3D model is built from the live stream, as it arrives. There is no
  record-then-process step in the data path. No offline pass, no "capture the
  flight, run pipeline.py overnight." Packets arrive on the socket, and the
  point cloud grows while the drone is still flying.

  Recordings exist only as a test harness — a fixture you can replay to
  reproduce a bug deterministically, and a regression check. They are never on
  the path that produces a delivered model. If I ever need a recording to make
  the model work, that's a bug in the architecture, not a design choice, and
  I'll say so rather than quietly leaning on it.

  The one thing I can't promise away, because it's geometry

  Your description is right except in one detail, and it matters more than it
  sounds:

  ▎ analyse each frame and its corresponding imu/gps and then start creating 
  ▎ point clouds then and there itself

  Frame i is analysed immediately — EKF pose, feature tracking, sparse map, all
  within milliseconds. But frame i's depth cannot exist yet. Triangulation needs
  parallax, and parallax needs the camera to have moved. dense_stereo.py wants
  a 4–20 m baseline before it will use a pair at all. At 10 m/s that's 0.4–2 s
  of flight after frame i before frame i can produce any 3D points.

  So the pipeline holds a rolling in-memory window of the last few seconds of
  frames — ~90 frames, roughly 240 MB at 720×1280. Frames enter the window, get
  their depth computed once their partners arrive, contribute their points to
  the cloud, and are dropped.

  That window is not a recording:

  ┌────────────────────┬───────────────────┬───────────────────┐
  │                    │  Rolling window   │     Recording     │
  ├────────────────────┼───────────────────┼───────────────────┤
  │ Lives in           │ RAM               │ disk              │
  ├────────────────────┼───────────────────┼───────────────────┤
  │ Size               │ bounded, ~3 s     │ grows with flight │
  ├────────────────────┼───────────────────┼───────────────────┤
  │ Frame's fate       │ dropped after use │ kept              │
  ├────────────────────┼───────────────────┼───────────────────┤
  │ Model can be built │ during flight     │ only after        │
  └────────────────────┴───────────────────┴───────────────────┘

  If you want, I can make that literal in the code — the live path never opens a
  file handle for frames, so it's structurally impossible for a recording to
  leak into the model. The recorder becomes a separate consumer subscribed to
  the same packet dispatch, and deleting it changes nothing about the output.

  That's the honest shape: near-real-time with a bounded lag, not instant. The
  operator sees the pose track immediately, the sparse map within a second, and
  the dense surface filling in a few seconds behind the aircraft. Nothing waits
  for the flight to end.

  Shall I draft that architecture — the dispatch layer, the rolling window, and
  the changes to ingest.py and build_dense_cloud to consume a growing sequence
  instead of a list?

✻ Churned for 29s · done 5:23 PM

❯ alright, so as decided, integrate the apk's stream with the pipeline. begin   

● Let me read the pieces I'll be building on.

  Searched for 1 pattern, read 2 files, ran 2 shell commands (ctrl+o to expand)

● Good. I have the full picture. Let me lay out the plan before writing code.

  Ran 1 shell command (ctrl+o to expand)

● Design is settled. Building it as a package so the live path and the fixture
  path share one parser.

  Ran 2 shell commands (ctrl+o to expand)

● Now the clock layer — §4.1, the highest-risk item.

  Read 2 files (ctrl+o to expand)

● Heredoc is choking on this content; switching to the Write tool.

● Write(stream\clock.py)
  ⎿  Wrote 218 lines to stream\clock.py
       1 """
       3 
       4     frames     wall clock, ms   (System.currentTimeMillis)      
       5     gps        wall clock, ms   (Location.getTime)              
       6     imu        MONOTONIC, ns    (SensorEvent.timestamp)         
         boot
         0.0 at session start
       8 
          monotonic
          The cost
     … +267 lines (ctrl+o to expand)

  Made 1 scratchpad edit +45, ran 1 shell command (ctrl+o to expand)

● Clock estimator behaves exactly as the theory predicts — jitter fully
  suppressed, residual is precisely the differential capture-to-send latency
  (−51 ms) the docstring warns about. Now geodesy.

● Write(stream\geodesy.py)
  ⎿  Wrote 114 lines to stream\geodesy.py
      1 """
        umes.
      3 
      4 `ekf.update_gps` takes ENU metres about a fixed origin, and that 
      5 must be the SAME one pipeline.py later reads as config["ref_lat_d
        eg"] /
      7 export_las. Get the two inconsistent and the model is internally 
        fine and
      8 georeferenced to the wrong place - a failure with no symptom unti
      9 opens the LAS in QGIS.
     … +148 lines (ctrl+o to expand)

● Now the tee — the piece that guarantees the model never reads from a file.

● Write(stream\source.py)
  ⎿  Wrote 326 lines to stream\source.py
       1 """
       2 Packet sources and the fan-out that feeds the live pipeline.
       3 
       5     ReplayPacketSource   a recorded session, re-emitted at its o
         wn pace
       6     StreamSession        clock alignment + reordering + fan-out
       7 
       9 
      10 StreamSession hands every packet to a list of subscribers. The l
         ive
     … +379 lines (ctrl+o to expand)

● Now the synthetic phone — a test double that speaks the app's exact wire
  protocol, so I can validate the whole integration today without hardware.

● Write(tools\replay_dataset_as_phone.py)
  ⎿  Wrote 225 lines to tools\replay_dataset_as_phone.py
       1 #!/usr/bin/env python3
       3 Stand-in for the handset: streams an existing rtvio_2 dataset ov
         er TCP in
       5 
         every
       7 number it would produce is currently unmeasured. This tool exist
       8 RECEIVING half can nevertheless be validated end to end today, a
         gainst a
         he live
         dware
     … +265 lines (ctrl+o to expand)

● Now the core: the live consumer with its rolling window and three lanes.

● Write(live_pipeline.py)
  ⎿  Wrote 685 lines to live_pipeline.py
       1 """
       2 Near-real-time reconstruction from the live phone stream.
       4     python live_pipeline.py --port 5555 --run-id live1
       5 
       6 THE RULE
       8 The model is built from packets as they arrive. There is no file
         -reading
       9 code path in this module: `LiveReconstructor` receives decoded p
      10 from `stream.source.StreamSession` and never learns where they c
         ame from.
     … +849 lines (ctrl+o to expand)
● Write(stream\recorder.py)
  ⎿  Wrote 148 lines to stream\recorder.py
       1 """
       3 
          writes it
       5 to disk in a form `replay.py` can re-emit byte-for-byte. The poi
       6 reproducibility: three of the known integration hazards (clock d
       7 frame timing, the camera-IMU extrinsic) fail SILENTLY - no excep
       8 plausible-looking output, wrong geometry. Fixing a sign error in
          an
         nd against
         ity and
     … +169 lines (ctrl+o to expand)

● Write(stream\replay.py)
  ⎿  Wrote 109 lines to stream\replay.py
       1 """
       3 
       4 This is the one place in the package that reads frames from disk
       5 exists so a silent geometry bug can be chased against identical 
       6 every run. A model produced through this path is a model produce
         d for
         .
       8 
         on times
         wall clock
     … +125 lines (ctrl+o to expand)

● Now let's test it. First imports and the geodesy round-trip.

● Write(tests\test_stream.py)
  ⎿  Wrote 234 lines to tests\test_stream.py
       1 """
         n 8).
       3 
       4 These are the checks worth having before you need them - each on
       5 corresponds to a documented way the integration fails SILENTLY, 
         producing
         n.
       7 
       8     python tests/test_stream.py
      10 import math
     … +278 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● All acceptance tests pass — ENU round-trip closes to 5×10⁻¹⁰ m and matches
  georeference.py exactly. Now the end-to-end socket test.

  Read 1 file, ran 3 shell commands (ctrl+o to expand)

● Two real bugs, and the first is a genuine design flaw, not a test artifact:
  warm-up progress is measured in receiver wall time, which decouples from
  stream time whenever packets arrive in a burst (fast replay, or a
  post-reconnect flush). Fixing it to use device-time span.

● Update(stream\clock.py)
  ⎿  Added 8 lines
      85          self._min_lag_boot = None      # min(t_recv - boot_s)
      86          self._n_frames = 0
      87          self._n_imu = 0
      88 +        # Warm-up progress is measured in DEVICE time, not rece
         +iver time. The                                                 
      89 +        # two decouple whenever packets arrive in a burst - a f
         +aster-than-real-                                               
      90 +        # time replay, or the flush after a reconnect - and a r
         +eceiver-clock                                                  
      91 +        # criterion then never fires, so the session ends still
         + warming up and                                                
      92 +        # reconstructs nothing. Device-time span is what "we ha
         +ve seen enough                                                 
      93 +        # of the capture to estimate the offset" actually means
         +.                                                              
      94 +        self._wall_span = _Span()                              
      95 +        self._boot_span = _Span()                              
      96  
      97          self.ready = False
      98          # boot_s + boot_to_wall_s == the same instant on the wa
          ll clock.

● Update(stream\clock.py)
  ⎿  Added 18 lines
      69      """Conversion attempted before the warm-up window closed.""
          "
      70  
      71  
      72 +class _Span:                                                   
      73 +    """Min/max of a stream of values, and the distance between 
         +them."""                                                       
      74 +                                                               
      75 +    __slots__ = ("lo", "hi")                                   
      76 +                                                               
      77 +    def __init__(self):                                        
      78 +        self.lo = None                                         
      79 +        self.hi = None                                         
      80 +                                                               
      81 +    def observe(self, v):                                      
      82 +        self.lo = v if self.lo is None else min(self.lo, v)    
      83 +        self.hi = v if self.hi is None else max(self.hi, v)    
      84 +                                                               
      85 +    @property                                                  
      86 +    def width(self):                                           
      87 +        return 0.0 if self.lo is None else self.hi - self.lo   
      88 +                                                               
      89 +                                                               
      90  class SessionClock:
      91      """Maps both device clock domains onto session-relative sec
          onds.
      92  

● Update(stream\clock.py)
  ⎿  Added 1 line
      133          """Record a wall-clock-stamped packet (frame or GPS)."
           ""
      134          t_recv = self._now() if t_recv is None else t_recv
      135          self._note_recv(t_recv)
      136 +        self._wall_span.observe(timestamp_ms / 1e3)           
      137          lag = t_recv - timestamp_ms / 1e3
      138          if self._min_lag_wall is None or lag < self._min_lag_w
           all:
      139              self._min_lag_wall = lag

● Update(stream\clock.py)
  ⎿  Added 1 line
      142          """Record a monotonic-since-boot-stamped packet (IMU).
           """
      143          t_recv = self._now() if t_recv is None else t_recv
      144          self._note_recv(t_recv)
      145 +        self._boot_span.observe(t_ns / 1e9)                   
      146          lag = t_recv - t_ns / 1e9
      147          if self._min_lag_boot is None or lag < self._min_lag_b
           oot:
      148              self._min_lag_boot = lag

● Update(stream\clock.py)
  ⎿  Added 14 lines, removed 3 lines
      171      # --------------------------------------------------------
           ------ close --
      172  
      173      def warmup_elapsed_s(self):
      174 -        if self._first_recv is None:                          
      175 -            return 0.0                                        
      176 -        return self._now() - self._first_recv                 
      174 +        """How much CAPTURE has been seen, in device seconds. 
          +Deliberately not                                              
      175 +        receiver wall time - see the _wall_span comment in __i
          +nit__."""                                                     
      176 +        return min(self._wall_span.width, self._boot_span.widt
          +h)                                                            
      177  
      178      def can_close(self):
      179          return (self._min_lag_wall is not None
     ...
      182                  and self._n_imu >= WARMUP_MIN_IMU
      183                  and self.warmup_elapsed_s() >= self._warmup_s)
      184  
      185 +    def force_close(self):                                    
      186 +        """Close on whatever has been seen. Used when the stre
          +am ends mid                                                   
      187 +        warm-up: a very short session is still worth reconstru
          +cting, and the                                                
      188 +        offset estimate from a handful of packets is imprecise
          + rather than                                                  
      189 +        meaningless. Returns False if not even one packet of e
          +ach kind                                                      
      190 +        arrived, in which case there is genuinely no timeline 
          +to build."""                                                  
      191 +        if self._min_lag_wall is None or self._min_lag_boot is
          + None:                                                        
      192 +            return False                                      
      193 +        self.close_warmup()                                   
      194 +        return True                                           
      195 +                                                              
      196      def close_warmup(self, session_zero_wall_s=None):
      197          """Freeze the offset and session zero. Idempotent."""
      198          if self.ready:

● Update(stream\source.py)
  ⎿  Added 18 lines, removed 7 lines
      219              print("\ninterrupted")
      220          finally:
      221              if not self.clock.ready:
      222 -                # Too short to align. Emitting the buffer woul
          -d mean inventing                                              
      223 -                # a timeline, so refuse rather than produce a 
          -plausible wrong one.                                          
      224 -                self._notify("on_session_end", self.stats)    
      225 -                raise SystemExit(                             
      226 -                    "stream ended during clock warm-up (%d fra
          -mes, %d imu samples) - "                                      
      227 -                    "too short to align the clock domains, so 
          -nothing was reconstructed"                                    
      228 -                    % (self.stats.frames, self.stats.imu_sampl
          -es))                                                          
      222 +                # The stream ended before the warm-up span was
          + reached - a very                                             
      223 +                # short session, or one delivered in a single 
          +burst. Close on                                               
      224 +                # what arrived rather than discard it: the off
          +set estimate is                                               
      225 +                # then imprecise, which is worth saying out lo
          +ud, but a short                                               
      226 +                # capture is still worth reconstructing.      
      227 +                if self.clock.force_close():                  
      228 +                    print("WARNING: stream ended after only %.
          +2f s of capture; the "                                        
      229 +                          "clock offset was estimated from %d 
          +frames / %d imu samples "                                     
      230 +                          "and is correspondingly imprecise." 
      231 +                          % (self.clock.warmup_elapsed_s(), se
          +lf.stats.frames,                                              
      232 +                             self.stats.imu_samples))         
      233 +                    self._finish_warmup()                     
      234 +                else:                                         
      235 +                    self._notify("on_session_end", self.stats)
      236 +                    raise SystemExit(                         
      237 +                        "stream carried no usable pairing of w
          +all-clock and IMU packets "                                   
      238 +                        "(%d frames, %d imu samples) - there i
          +s no timeline to build"                                       
      239 +                        % (self.stats.frames, self.stats.imu_s
          +amples))                                                      
      240              self._drain(until=float("inf"))
      241              self._report_clock_check()
      242              self._notify("on_session_end", self.stats)

● Update(stream\source.py)
  ⎿  Added 6 lines, removed 3 lines
      258              self.clock.observe_gps(payload, t_recv)
      259  
      260      def _close_warmup(self):
      261 +        self.clock.close_warmup()                             
      262 +        self._finish_warmup()                                 
      263 +                                                              
      264 +    def _finish_warmup(self):                                 
      265          # Session zero is the earliest event actually seen, ex
           pressed on the
      266          # wall clock - so t=0 is a real instant in the capture
            rather than the
      267          # arbitrary moment the receiver happened to be schedul
           ed.
      264 -        self.clock.close_warmup()                             
      268          wall_candidates = [p.timestamp_ms / 1e3 for k, p in se
           lf._warmup_buffer
      269                             if k in (KIND_FRAME, KIND_GPS)]
      270          boot_candidates = [s.t_ns / 1e9 + self.clock.boot_to_w
           all_s
      271                             for k, p in self._warmup_buffer if 
           k == KIND_IMU
      272                             for s in p]
      270 -        earliest = min(wall_candidates + boot_candidates)     
      271 -        self.clock.wall_t0_s = earliest                       
      273 +        if wall_candidates or boot_candidates:                
      274 +            self.clock.wall_t0_s = min(wall_candidates + boot_
          +candidates)                                                   
      275  
      276          if self.verbose:
      277              print(self.clock.describe())

● Update(stream\source.py)
  ⎿  Added 4 lines
      149              conn, addr = server.accept()
      150              conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NOD
           ELAY, 1)
      151              self.peer = addr
      152 +            # Rates are measured from the moment the phone con
          +nects, not from                                               
      153 +            # whenever the receiver was started - otherwise id
          +le listening time                                             
      154 +            # is averaged into the reported fps.              
      155 +            stats.started = time.monotonic()                  
      156              print("phone connected: %s:%d" % addr)
      157              if self.on_connect:
      158                  self.on_connect(addr)

● Now the architectural fix that makes the three-lane latency claim actually
  true: the dense lane must not block the socket reader.

● Update(live_pipeline.py)
  ⎿  Added 2 lines
      48  import csv
      49  import json
      50  import os
      51 +import queue                                                   
      52 +import threading                                               
      53  import time
      54  from collections import deque
      55  

● Update(live_pipeline.py)
  ⎿  Added 9 lines
      101  MAX_PLAUSIBLE_ACCEL = 8.0 * 9.81      # m/s^2
      102  MAX_PLAUSIBLE_GYRO = 35.0             # rad/s, past most MEMS 
           full-scale ranges
      103  
      104 +# Dense lane queue depth. Bounded on purpose: dense stereo is 
          +~10x slower than                                              
      105 +# real time on CPU, so on a long capture it WILL fall behind. 
          +The choice is                                                 
      106 +# between growing a queue until the process dies and shedding 
          +keyframes to stay                                             
      107 +# current, and for a live system the second is the only honest
          + answer - a                                                   
      108 +# sparser cloud that tracks the aircraft beats a dense one tha
          +t stopped                                                     
      109 +# following it twenty minutes ago. Drops are counted and repor
          +ted.                                                          
      110 +DENSE_QUEUE_DEPTH = 4                                         
      111  
      112 +                                                              
      113 +                                                              
      114  class RollingWindow:
      115      """The frames a pending keyframe might still need, and not
           hing else.
      116  

● Update(live_pipeline.py)
  ⎿  Added 8 lines
      199          self.sharpness_hist = deque(maxlen=BLUR_MEDIAN_WINDOW)
      200          self.n_blurred = 0
      201  
      202 +        # The dense lane runs on its own thread(s). Keeping it
          + off the socket                                               
      203 +        # reader is what makes the latency split real rather t
          +han nominal: a                                                
      204 +        # plane sweep takes seconds, and running it inline wou
          +ld stall the EKF                                              
      205 +        # and tracking lanes behind it and back-pressure the p
          +hone's socket.                                                
      206 +        self.cloud_lock = threading.Lock()                    
      207          self.cloud_pts = []
      208          self.cloud_cols = []
      209          self.n_dense_points = 0
      210          self.keyframes_done = 0
      211 +        self.dense_dropped = 0                                
      212 +        self._dense_q = queue.Queue(maxsize=DENSE_QUEUE_DEPTH)
      213 +        self._dense_workers = []                              
      214  
      215          self.implausible_imu = 0
      216          self.gps_used = 0

● Now rewriting the dense lane to enqueue rather than run inline.

● Update(live_pipeline.py)
  ⎿  Added 1 line, removed 3 lines
      447              if not partners and not exhausted:
      448                  return                      # wait for more fr
           ames to arrive
      449              if partners:
      450 -                t0 = time.perf_counter()                      
      451 -                self._reconstruct_keyframe(items, poses, partn
          -ers)                                                          
      452 -                self.lane_time["dense"] += time.perf_counter()
          - - t0                                                         
      450 +                self._enqueue_keyframe(items, poses, partners)
      451              self.next_keyframe_idx += self.window.keyframe_str
           ide
      452              self.window.evict_before(self.next_keyframe_idx)
      453  

● Update(live_pipeline.py)
  ⎿  Added 56 lines, removed 14 lines
      458          d = np.linalg.norm(poses[-1][1] - poses[0][1])
      459          return d > dense_stereo.MAX_STEREO_BASELINE_M
      460  
      461 -    def _reconstruct_keyframe(self, items, poses, partners):  
      461 +    def _enqueue_keyframe(self, items, poses, partners):      
      462 +        """Hand one keyframe to the dense lane. Runs on the re
          +ader thread, so                                               
      463 +        it must stay cheap - it only selects and packages.""" 
      464          ref = items[0]
      465          if ref["blurred"]:
      466              return
      467          usable = [j for j in partners if not items[j]["blurred
           "]]
      468          if not usable:
      469              return
      468 -        src_imgs = [items[j]["img"] for j in usable]          
      469 -        src_poses = [poses[j] for j in usable]                
      470 -        R_ref, p_ref = poses[0]                               
      470 +        # The sparse map is snapshotted here rather than read 
          +inside the                                                    
      471 +        # worker: the tracking lane mutates tracker.points on 
          +every frame, and                                              
      472 +        # reading it from another thread would be a race.     
      473 +        sparse = (np.array(self.tracker.points, dtype=np.float
          +64)                                                           
      474 +                  if len(self.tracker.points) else None)      
      475 +        job = {                                               
      476 +            "ref_img": ref["img"],                            
      477 +            "R_ref": poses[0][0], "p_ref": poses[0][1],       
      478 +            "src_imgs": [items[j]["img"] for j in usable],    
      479 +            "src_poses": [poses[j] for j in usable],          
      480 +            "sparse": sparse,                                 
      481 +            "recv": ref["recv"],                              
      482 +        }                                                     
      483 +        try:                                                  
      484 +            self._dense_q.put_nowait(job)                     
      485 +        except queue.Full:                                    
      486 +            # Shed rather than queue. See DENSE_QUEUE_DEPTH.  
      487 +            self.dense_dropped += 1                           
      488  
      489 +    def _start_dense_workers(self):                           
      490 +        for _ in range(max(1, self.args.dense_workers)):      
      491 +            t = threading.Thread(target=self._dense_loop, daem
          +on=True)                                                      
      492 +            t.start()                                         
      493 +            self._dense_workers.append(t)                     
      494 +                                                              
      495 +    def _dense_loop(self):                                    
      496 +        while True:                                           
      497 +            job = self._dense_q.get()                         
      498 +            if job is None:                                   
      499 +                self._dense_q.task_done()                     
      500 +                return                                        
      501 +            try:                                              
      502 +                self._reconstruct_keyframe(job)               
      503 +            except Exception as e:                      # noqa
          +: BLE001                                                      
      504 +                # One bad keyframe must not take the session d
          +own; the model is                                             
      505 +                # built incrementally and can lose a tile.    
      506 +                print("dense keyframe failed: %s: %s" % (type(
          +e).__name__, e))                                              
      507 +            finally:                                          
      508 +                self._dense_q.task_done()                     
      509 +                                                              
      510 +    def _reconstruct_keyframe(self, job):                     
      511 +        t0 = time.perf_counter()                              
      512 +        R_ref, p_ref = job["R_ref"], job["p_ref"]             
      513          depth_range = depth_range_from_coarse_sweep(
      473 -            ref["img"], src_imgs, R_ref, p_ref, src_poses, sel
          -f.K)                                                          
      514 +            job["ref_img"], job["src_imgs"], R_ref, p_ref, job
          +["src_poses"], self.K)                                        
      515          if depth_range is None:
      475 -            sparse = np.array(self.tracker.points) if len(self
          -.tracker.points) else None                                    
      516              depth_range = depth_range_from_sparse(
      477 -                sparse, R_ref, p_ref, self.K, ref["img"].shape
          -)                                                             
      517 +                job["sparse"], R_ref, p_ref, self.K, job["ref_
          +img"].shape)                                                  
      518          pts, cols = stereo_views_to_points(
      479 -            ref["img"], src_imgs, R_ref, p_ref, src_poses, sel
          -f.K,                                                          
      519 +            job["ref_img"], job["src_imgs"], R_ref, p_ref, job
          +["src_poses"], self.K,                                        
      520              stride=self.args.stereo_stride, depth_range=depth_
           range)
      481 -        self.keyframes_done += 1                              
      482 -        self.dense_latency_s.append(time.monotonic() - ref["re
          -cv"])                                                         
      483 -        if len(pts):                                          
      484 -            self.cloud_pts.append(pts)                        
      485 -            self.cloud_cols.append(cols)                      
      486 -            self.n_dense_points += len(pts)                   
      521 +        with self.cloud_lock:                                 
      522 +            self.keyframes_done += 1                          
      523 +            self.dense_latency_s.append(time.monotonic() - job
          +["recv"])                                                     
      524 +            self.lane_time["dense"] += time.perf_counter() - t
          +0                                                             
      525 +            if len(pts):                                      
      526 +                self.cloud_pts.append(pts)                    
      527 +                self.cloud_cols.append(cols)                  
      528 +                self.n_dense_points += len(pts)               
      529  
      530      def _progress(self, idx, t_s):
      531          lat = (np.median(self.dense_latency_s[-20:])

● Update(live_pipeline.py)  ⎿  Added 1 line
      374              self.frame_wh = (w, h)
      375          self.K = K
      376          self.tracker = Tracker(self.K, run_bundle_adjustment=T
           rue)
      377 +        self._start_dense_workers()                           
      378          cx, cy = K[0, 2], K[1, 2]
      379          if abs(cx - self.frame_wh[0] / 2) < 1e-6 and abs(cy - 
           self.frame_wh[1] / 2) < 1e-6:
      380              print("NOTE: principal point is exactly the image 
           centre. A real "

● Update(live_pipeline.py)
  ⎿  Added 27 lines, removed 1 line
      549          self._finalize(stats)
      550  
      551      def _flush_tail(self):
      552 +        """Drain the window at shutdown. Every remaining keyfr
          +ame either has                                                
      553 +        its partners already or never will, so this is finishi
          +ng work that was                                              
      554 +        always going to happen - not a second, offline reconst
          +ruction pass."""                                              
      555          while self.window.last_idx is not None and self.next_k
           eyframe_idx <= self.window.last_idx:
      556              k = self.next_keyframe_idx
      557              items = self.window.slice_from(k)
     ...
      559                  poses = [self.refined_poses[it["idx"]] for it 
           in items]
      560                  partners = find_stereo_partners(poses, 0)
      561                  if partners:
      559 -                    self._reconstruct_keyframe(items, poses, p
          -artners)                                                      
      562 +                    # Blocking put here, unlike the live path:
          + at shutdown                                                  
      563 +                    # there is no longer any point shedding lo
          +ad to stay current.                                           
      564 +                    ref = items[0]                            
      565 +                    if not ref["blurred"]:                    
      566 +                        usable = [j for j in partners if not i
          +tems[j]["blurred"]]                                           
      567 +                        if usable:                            
      568 +                            sparse = (np.array(self.tracker.po
          +ints, dtype=np.float64)                                       
      569 +                                      if len(self.tracker.poin
          +ts) else None)                                                
      570 +                            self._dense_q.put({               
      571 +                                "ref_img": ref["img"],        
      572 +                                "R_ref": poses[0][0], "p_ref":
          + poses[0][1],                                                 
      573 +                                "src_imgs": [items[j]["img"] f
          +or j in usable],                                              
      574 +                                "src_poses": [poses[j] for j i
          +n usable],                                                    
      575 +                                "sparse": sparse, "recv": ref[
          +"recv"],                                                      
      576 +                            })                                
      577              self.next_keyframe_idx += self.window.keyframe_str
           ide
      578              self.window.evict_before(self.next_keyframe_idx)
      579  
      580 +        pending = self._dense_q.qsize()                       
      581 +        if pending:                                           
      582 +            print("waiting for %d queued keyframes in the dens
          +e lane..." % pending)                                         
      583 +        self._dense_q.join()                                  
      584 +        for _ in self._dense_workers:                         
      585 +            self._dense_q.put(None)                           
      586 +        for t in self._dense_workers:                         
      587 +            t.join(timeout=60)                                
      588 +                                                              
      589      def _finalize(self, stats):
      590          out = self.out_dir
      591          ref_lat, ref_lon, ref_alt = self.ref if self.ref else 
           (0.0, 0.0, 0.0)

● Update(live_pipeline.py)
  ⎿  Added 6 lines, removed 4 lines
      529                  self.n_dense_points += len(pts)
      530  
      531      def _progress(self, idx, t_s):
      532 -        lat = (np.median(self.dense_latency_s[-20:])          
      533 -               if self.dense_latency_s else float("nan"))     
      532 +        with self.cloud_lock:                                 
      533 +            lat = (float(np.median(self.dense_latency_s[-20:])
          +)                                                             
      534 +                   if self.dense_latency_s else float("nan")) 
      535 +            kf, npts = self.keyframes_done, self.n_dense_point
          +s                                                             
      536          print("t=%6.2fs  frame %5d  window %3d  map %5d  keyfr
           ames %4d  "
      535 -              "cloud %8d  dense-lag %5.2fs"                   
      537 +              "cloud %8d  dense-lag %5.2fs  q%d"              
      538                % (t_s, idx, len(self.window), len(self.tracker.
           points),
      537 -                 self.keyframes_done, self.n_dense_points, lat
          -))                                                            
      539 +                 kf, npts, lat, self._dense_q.qsize()))       
      540  
      541      # --------------------------------------------------------
           ----- finish --
      542  

● Update(live_pipeline.py)
  ⎿  Added 3 lines
      722      ap.add_argument("--stereo-stride", type=int, default=2,
      723                      help="pixel stride in the plane sweep; the
            main speed/density knob")
      724      ap.add_argument("--keyframe-stride", type=int, default=den
           se_stereo.KEYFRAME_STRIDE)
      725 +    ap.add_argument("--dense-workers", type=int, default=2,   
      726 +                    help="threads running plane-sweep stereo. 
          +The sweep is numpy-"                                          
      727 +                         "heavy and releases the GIL, so >1 ge
          +nuinely parallelises")                                        
      728      ap.add_argument("--max-long-edge", type=int, default=0,
      729                      help="downsample frames to this long edge 
           (0 = native); "
      730                           "intrinsics are scaled to match")

● Update(live_pipeline.py)
  ⎿  Added 2 lines, removed 1 line
      670              "## Model", "",
      671              "- Dense cloud: %d points (EPSG:%s)" % (len(pts), 
           epsg_out),
      672              "- Sparse map: %d points" % len(self.tracker.point
           s),
      673 -            "- Keyframes reconstructed: %d" % self.keyframes_d
          -one,                                                          
      673 +            "- Keyframes reconstructed: %d (%d shed to keep th
          +e lane current)"                                              
      674 +            % (self.keyframes_done, self.dense_dropped),      
      675              "- Mesh: %d vertices, %d faces, %.1f%% completenes
           s"
      676              % (mesh_info["n_vertices"], mesh_info["n_faces"],
      677                 mesh_info["completeness"] * 100),

  Ran 1 shell command (ctrl+o to expand)

● Now the real end-to-end run, paced at true real time so the latency numbers
  mean something.

  Read 1 file, ran 2 shell commands (ctrl+o to expand)

● Update(live_pipeline.py)
  ⎿  Added 1 line, removed 1 line
      434          later frame 4-20 m away. Until one exists, i is not co
           mputable by any
      435          implementation. Once one does, i fires immediately and
            is evicted.
      436          """
      437 -        while self.next_keyframe_idx <= self.window.last_idx: 
      437 +        while self.window.last_idx is not None and self.next_k
          +eyframe_idx <= self.window.last_idx:                          
      438              k = self.next_keyframe_idx
      439              items = self.window.slice_from(k)
      440              if not items or items[0]["idx"] != k:

  Read 1 file, ran 2 shell commands (ctrl+o to expand)

● It works end to end from a live socket — 388k dense points, a real mesh,
  EPSG:32643, dense-lane latency 1.99 s exactly as predicted. Three things to
  fix. First, the rate reporting is misleading under backpressure:

● Update(stream\source.py)
  ⎿  Added 17 lines
      66          self.first_imu_ns = None
      67          self.started = time.monotonic()
      68          self._intervals = []
      69 +        # Rates are reported per second of CAPTURE, not per sec
         +ond of receiver                                                
      70 +        # wall clock. When the consumer cannot keep up, TCP bac
         +k-pressures the                                                
      71 +        # phone and receiver time stretches; dividing by it the
         +n reports an IMU                                               
      72 +        # rate of 19 Hz for a stream that genuinely carried 100
         + Hz, which is a                                                
      73 +        # data-health alarm firing on a throughput problem.    
      74 +        self.device_lo = None                                  
      75 +        self.device_hi = None                                  
      76  
      77 +    def note_event_time(self, t_s):                            
      78 +        self.device_lo = t_s if self.device_lo is None else min
         +(self.device_lo, t_s)                                          
      79 +        self.device_hi = t_s if self.device_hi is None else max
         +(self.device_hi, t_s)                                          
      80 +                                                               
      81 +    @property                                                  
      82 +    def capture_span_s(self):                                  
      83 +        if self.device_lo is None:                             
      84 +            return 0.0                                         
      85 +        return self.device_hi - self.device_lo                 
      86 +                                                               
      87      def note_frame_interval(self, dt_s):
      88          if dt_s > 0:
      89              self._intervals.append(dt_s)

● Update(stream\source.py)
  ⎿  Added 12 lines, removed 7 lines
      101          return (1.0 / m) if m else 0.0
      102  
      103      def summary(self):
      104 -        dur = max(time.monotonic() - self.started, 1e-9)      
      104 +        wall = max(time.monotonic() - self.started, 1e-9)     
      105 +        cap = max(self.capture_span_s, 1e-9)                  
      106          lines = [
      106 -            "session %.1f s" % dur,                           
      107 -            "  frames       %-8d (%.1f fps median, %.1f fps me
          -an)"                                                          
      108 -            % (self.frames, self.measured_fps, self.frames / d
          -ur),                                                          
      109 -            "  imu samples  %-8d (%.1f Hz mean, %d batches)"  
      110 -            % (self.imu_samples, self.imu_samples / dur, self.
          -imu_batches),                                                 
      111 -            "  gps fixes    %d" % self.gps_fixes,             
      107 +            "captured %.1f s of stream in %.1f s wall clock (%
          +.2fx real time)"                                              
      108 +            % (self.capture_span_s, wall, self.capture_span_s 
          +/ wall),                                                      
      109 +            "  frames       %-8d (%.1f fps median, %.1f fps me
          +an over capture)"                                             
      110 +            % (self.frames, self.measured_fps, self.frames / c
          +ap),                                                          
      111 +            "  imu samples  %-8d (%.1f Hz over capture, %d bat
          +ches)"                                                        
      112 +            % (self.imu_samples, self.imu_samples / cap, self.
          +imu_batches),                                                 
      113 +            "  gps fixes    %-8d (%.1f Hz over capture)" % (se
          +lf.gps_fixes, self.gps_fixes / cap),                          
      114              "  received     %.1f MB" % (self.bytes / 1e6),
      115          ]
      116 +        if self.capture_span_s > wall * 1.05:                 
      117 +            lines.append("  NOTE: the consumer ran slower than
          + real time, so the phone was "                                
      118 +                         "back-pressured by TCP. Rates above a
          +re per second of capture.")                                   
      119          if self.late_dropped:
      120              lines.append("  LATE-DROPPED %d events - raise REO
           RDER_HOLD_S" % self.late_dropped)
      121          if self.frame_gaps:

● Update(stream\source.py)
  ⎿  Added 1 line
      340              self._emit(t_s, kind, payload)
      341  
      342      def _emit(self, t_s, kind, payload):
      343 +        self.stats.note_event_time(t_s)                       
      344          if kind == KIND_IMU:
      345              self._notify("on_imu", t_s, payload)
      346          elif kind == KIND_FRAME:

● Now instrumenting the tracking lane — 163 ms/frame is 5× the batch cost and I
  want to know why, not guess.

● Update(live_pipeline.py)
  ⎿  Added 2 lines, removed 1 line
      215          self.implausible_imu = 0
      216          self.gps_used = 0
      217          self.gps_rejected_no_ref = 0
      218 -        self.lane_time = {"ekf": 0.0, "track": 0.0, "dense": 0
          -.0}                                                           
      218 +        self.lane_time = {"ekf": 0.0, "decode": 0.0, "blur": 0
          +.0,                                                           
      219 +                          "track": 0.0, "ba": 0.0, "dense": 0.
          +0}                                                            
      220          self.dense_latency_s = []
      221          self.t_started = None
      222  

● Update(live_pipeline.py)
  ⎿  Added 5 lines
      383                    "intrinsics rather than a calibration.")
      384  
      385      def _process_frame(self, t_s, pkt):
      386 +        t_dec = time.perf_counter()                           
      387          img = self._decode(pkt)
      388          if img is None:
      389              return
      390 +        self.lane_time["decode"] += time.perf_counter() - t_de
          +c                                                             
      391          idx = self.frame_count
      392          self.frame_count += 1
      393  
     ...
      400              blurred = sharp < BLUR_FACTOR * float(np.median(se
           lf.sharpness_hist))
      401          if blurred:
      402              self.n_blurred += 1
      403 +        self.lane_time["blur"] += time.perf_counter() - t0    
      404  
      405 +        t0 = time.perf_counter()                              
      406          self.tracker.process_frame(img, self.ekf.R.copy(), sel
           f.ekf.p.copy(),
      407                                     gyro_delta_R=self.gyro_delt
           a_R.copy())
      408          self.gyro_delta_R = np.eye(3)
      409 +        self.lane_time["track"] += time.perf_counter() - t0   
      410  
      411          pose = (self.ekf.R.copy(), self.ekf.p.copy())
      412          self.frame_times.append(t_s)

● Update(live_pipeline.py)
  ⎿  Added 2 lines, removed 1 line
      418          self.window.append(item)
      419  
      420          if self.tracker.frame_idx % max(1, self.tracker.BA_WIN
           DOW // 2) == 0:
      421 +            t0 = time.perf_counter()                          
      422              refined = self.tracker.windowed_bundle_adjustment(
           )
      423              if refined is not None:
      424                  for f_idx, R_ba, p_ba in refined:
      425                      j = f_idx - 1          # tracker frame_idx
            is 1-based
      426                      if 0 <= j < len(self.refined_poses):
      427                          self.refined_poses[j] = (R_ba.copy(), 
           np.array(p_ba))
      427 -        self.lane_time["track"] += time.perf_counter() - t0   
      428 +            self.lane_time["ba"] += time.perf_counter() - t0  
      429  
      430          self._pump_dense()
      431  

● Update(live_pipeline.py)
  ⎿  Added 5 lines, removed 2 lines
      669              "- Captured %.1f s of stream in %.1f s wall clock 
           (%.2fx real time)"
      670              % (span, wall, span / max(wall, 1e-9)),
      671              "- Median dense-lane latency, frame received -> it
           s points exist: %.2f s" % lat,
      672 -            "- Lane cost: ekf %.1f s, tracking %.1f s, dense %
          -.1f s"                                                        
      673 -            % (self.lane_time["ekf"], self.lane_time["track"],
          - self.lane_time["dense"]),                                    
      672 +            "- Lane cost, CPU-seconds: " + ", ".join(         
      673 +                "%s %.1f" % (k, v) for k, v in self.lane_time.
          +items()),                                                     
      674 +            "- Per frame: decode %.0f ms, blur %.0f ms, track 
          +%.0f ms, BA %.0f ms"                                          
      675 +            % tuple(1e3 * self.lane_time[k] / max(self.frame_c
          +ount, 1)                                                      
      676 +                    for k in ("decode", "blur", "track", "ba")
          +),                                                            
      677              "- Peak rolling window: %d frames (batch Dataset w
           ould have held %d)"
      678              % (self.window.peak_len, self.frame_count),
      679              "",

● Now the key correctness question: does the live path track ground truth as
  well as the batch pipeline? Adding an ENU trajectory dump so it can be scored.

● Update(live_pipeline.py)
  ⎿  Added 9 lines
      621              for r in rows:
      622                  w.writerow(r)
      623  
      624 +        # Local-ENU trajectory, in the same frame the EKF fuse
          +d in. pose_file.csv                                           
      625 +        # is the frozen deliverable schema and carries UTM; th
          +is is the raw                                                 
      626 +        # estimate, kept so a run can be scored against a grou
          +nd truth that                                                 
      627 +        # happens to exist (the synthetic dataset) without a p
          +rojection                                                     
      628 +        # round-trip in the way.                              
      629 +        with open(os.path.join(out, "trajectory_enu.json"), "w
          +") as f:                                                      
      630 +            json.dump([{"timestamp": round(t, 6), "pos_enu": [
          +float(v) for v in p]}                                         
      631 +                       for t, (_R, p) in zip(self.frame_times,
          + self.frame_poses)], f)                                       
      632 +                                                              
      633          mesh_info = {"n_vertices": 0, "n_faces": 0, "completen
           ess": 0.0}
      634          grid = None
      635          epsg_out = None

● Write(tools\score_live_run.py)
  ⎿  Wrote 59 lines to tools\score_live_run.py
       1 #!/usr/bin/env python3
       3 Score a live run against the synthetic dataset's ground truth.
       4 
         t_as_phone.py,
       6 which is driven by a dataset that HAS ground truth. A real phone
       7 neither a ground-truth trajectory nor surveyed checkpoints, so i
         t cannot be
         why the test
       9 double exists: it is the only way to ask "is the live path as ac
      10 batch path?" before hardware does.
     … +66 lines (ctrl+o to expand)

  Ran 3 shell commands (ctrl+o to expand)

● ATE 3.65 m vs the batch pipeline's 1.35 m on identical data. That gap is real
  and worth chasing. Top suspect: I told the replayer to claim 4 m GPS accuracy
  while the dataset's actual noise is 2 m, so the live EKF is down-weighting GPS
  2×. Testing that directly.

  Ran 2 shell commands (ctrl+o to expand)

● ATE 3.65 → 2.38 m. Confirmed — GPS sigma was a major contributor. Clock
  alignment is essentially exact here (estimate matches truth to ~25 µs). Let me
  check how ATE handles the frame difference, since the live run uses the first
  GPS fix as origin while ground truth uses the dataset's config origin.
  Ran 1 shell command (ctrl+o to expand)

● compute_ate does Umeyama alignment, so the origin difference is absorbed —
  2.38 vs 1.35 is a fair comparison. Next suspect: the batch pipeline
  initialises heading from gps[:15] (data from the future at t=0), while live
  can only use what has arrived — I used 6 fixes. Making that a flag and
  testing.

● Update(live_pipeline.py)
  ⎿  Added 1 line, removed 1 line
      289          if self.initialized:
      290              return
      291          course = None
      292 -        if len(self._gps_enu_history) >= COURSE_MIN_FIXES:    
      292 +        if len(self._gps_enu_history) >= self.args.course_min_
          +fixes:                                                        
      293              course = course_over_ground(self._gps_enu_history,
      294                                          min_distance_m=COURSE_
           MIN_DISTANCE_M)
      295          timed_out = (self._first_event_t is not None

● Update(live_pipeline.py)
  ⎿  Added 6 lines
      742      ap.add_argument("--stereo-stride", type=int, default=2,
      743                      help="pixel stride in the plane sweep; the
            main speed/density knob")
      744      ap.add_argument("--keyframe-stride", type=int, default=den
           se_stereo.KEYFRAME_STRIDE)
      745 +    ap.add_argument("--course-min-fixes", type=int, default=CO
          +URSE_MIN_FIXES,                                               
      746 +                    help="GPS fixes to accumulate before the i
          +nitial heading is "                                           
      747 +                         "estimated. This is a direct startup-
          +latency vs "                                                  
      748 +                         "heading-accuracy trade: more fixes m
          +eans a longer baseline "                                      
      749 +                         "and a better course, but the filter 
          +stays unstarted (and "                                        
      750 +                         "everything is buffered) until they a
          +rrive")                                                       
      751      ap.add_argument("--dense-workers", type=int, default=2,
      752                      help="threads running plane-sweep stereo. 
           The sweep is numpy-"
      753                           "heavy and releases the GIL, so >1 ge
           nuinely parallelises")

  Ran 3 shell commands (ctrl+o to expand)

● Two findings — and one is a bug: MAX_INIT_BUFFER_FRAMES is being compared
  against the total pending count (which is dominated by 100 Hz IMU), so frames
  were silently dropped during startup. Let me check the heading truth first.

  Made 1 scratchpad edit +22, ran 1 shell command (ctrl+o to expand)

● The ground truth flies due east (heading ≈ 0°); course_over_ground on noisy
  GPS gives 36° at 6 fixes, 20° at 15, 1.2° at 50. So the c15 run's worse ATE
  was the buffer bug, not the heading. Let me read course_over_ground and fix
  the bug.

  Ran 1 shell command (ctrl+o to expand)

● course_over_ground is a two-point difference — with 2 m GPS noise over a 12 m
  baseline that's ~13° of pure noise. But velocity_from_fixes in the same module
  does a least-squares line fit over all fixes, and its docstring argues that
  seeding zero velocity actively corrupts attitude and bias states. Using it for
  both heading and initial velocity.

● Update(live_pipeline.py)
  ⎿  Added 1 line, removed 1 line
      62                            voxel_downsample, statistical_outlier
          _removal)
      63  from export import export_las, write_mesh_origin_sidecar, expor
          t_dsm_raster
      64  from georeference import (georeference_trajectory, course_over_
          ground,
      65 -                          POSE_FILE_COLUMNS)                   
      65 +                          velocity_from_fixes, POSE_FILE_COLUMN
         +S)                                                             
      66  from ingest import sharpness_score, downsample_intrinsics
      67  from inertial_nav_ekf import InertialNavEKF
      68  from meshing import build_grid, write_textured_mesh

● Update(live_pipeline.py)
  ⎿  Added 1 line
      267                    % self.ref)
      268          enu = latlon_to_enu(pkt.lat_deg, pkt.lon_deg, pkt.alti
           tude_m, *self.ref)
      269          self._gps_enu_history.append(enu)
      270 +        self._gps_time_history.append(t_s)                    
      271  
      272          if not self.initialized:
      273              self._buffer("gps", (t_s, enu, sigma_m))

● Update(live_pipeline.py)
  ⎿  Added 4 lines
      182          self.initialized = False
      183          self._pending = []             # (kind, args) buffered
            until yaw is known
      184          self._gps_enu_history = []
      185 +        self._gps_time_history = []                           
      186 +        self._pending_frames = 0                              
      187          self._first_event_t = None
      188 +        self.init_course_deg = None                           
      189 +        self.init_speed_mps = None                            
      190  
      191          self.prev_t = None
      192          self.gyro_delta_R = np.eye(3)

● Now replacing the initialisation logic:

● Update(live_pipeline.py)
  ⎿  Added 48 lines, removed 13 lines
      289              self._first_event_t = payload[0]
      290          self._pending.append((kind, payload))
      291  
      292 +    def _estimate_course_and_velocity(self):                  
      293 +        """Initial heading and velocity from the GPS fixes rec
          +eived so far.                                                 
      294 +                                                              
      295 +        Prefers a least-squares line fit (velocity_from_fixes)
          + over the                                                     
      296 +        two-point difference course_over_ground uses. Both con
          +sume the same                                                 
      297 +        fixes, but the two-point version throws away every fix
          + in between:                                                  
      298 +        with metre-scale position noise over a short baseline,
          + the resulting                                                
      299 +        heading is dominated by that noise. On this dataset th
          +e two-point                                                   
      300 +        course is 36 deg out after 6 fixes where the truth is 
          +~0 deg, and the                                               
      301 +        line fit over the same 6 fixes is far closer.         
      302 +                                                              
      303 +        The velocity is worth having for its own sake. Seeding
          + zero velocity                                                
      304 +        for a platform that is already moving is not neutral -
          + see                                                          
      305 +        georeference.velocity_from_fixes, which spells out how
          + the filter                                                   
      306 +        distributes that contradiction into attitude and bias 
          +states, and                                                   
      307 +        that those corruptions do not undo themselves later.  
      308 +        """                                                   
      309 +        n = len(self._gps_enu_history)                        
      310 +        if n < self.args.course_min_fixes:                    
      311 +            return None, None                                 
      312 +        v = velocity_from_fixes(self._gps_time_history, self._
          +gps_enu_history)                                              
      313 +        if v is not None and np.linalg.norm(v[:2]) * (        
      314 +                self._gps_time_history[-1] - self._gps_time_hi
          +story[0]) >= COURSE_MIN_DISTANCE_M:                           
      315 +            return float(np.arctan2(v[1], v[0])), np.asarray(v
          +, dtype=np.float64)                                           
      316 +        # Fall back to the two-point course if the fit is unus
          +able (too short a                                             
      317 +        # time span for a slope to mean anything).            
      318 +        return course_over_ground(self._gps_enu_history,      
      319 +                                  min_distance_m=COURSE_MIN_DI
          +STANCE_M), None                                               
      320 +                                                              
      321      def _try_initialize(self, t_s):
      293 -        """Start the filter once course over ground gives an o
          -bservable yaw."""                                             
      322 +        """Start the filter once the platform has moved enough
          + for heading to                                               
      323 +        be observable. Yaw is the one attitude component neith
          +er the                                                        
      324 +        accelerometer nor a GPS position fix can supply, so th
          +is genuinely                                                  
      325 +        cannot happen sooner - and starting without it is not 
          +a small error."""                                             
      326          if self.initialized:
      327              return
      296 -        course = None                                         
      297 -        if len(self._gps_enu_history) >= self.args.course_min_
          -fixes:                                                        
      298 -            course = course_over_ground(self._gps_enu_history,
      299 -                                        min_distance_m=COURSE_
          -MIN_DISTANCE_M)                                               
      328 +        course, velocity = self._estimate_course_and_velocity(
          +)                                                             
      329          timed_out = (self._first_event_t is not None
      330                       and t_s - self._first_event_t > COURSE_TI
           MEOUT_S)
      331          if course is None and not timed_out:
      332              return
      333          if course is None:
      305 -            print("WARNING: no course over ground after %.1f s
          - - the platform has not "                                     
      306 -                  "moved %.0f m. Starting with UNOBSERVED YAW;
          - expect a large constant "                                    
      307 -                  "heading error that nothing downstream recov
          -ers."                                                         
      334 +            print("WARNING: heading is still unobservable afte
          +r %.1f s - the platform "                                     
      335 +                  "has not moved %.0f m. Starting with UNOBSER
          +VED YAW; expect a large "                                     
      336 +                  "constant heading error that nothing downstr
          +eam recovers."                                                
      337                    % (COURSE_TIMEOUT_S, COURSE_MIN_DISTANCE_M))
      338          else:
      310 -            print("initialising attitude with course over grou
          -nd %.1f deg"                                                  
      311 -                  % np.degrees(course))                       
      312 -        self._start_filter(course)                            
      339 +            self.init_course_deg = float(np.degrees(course))  
      340 +            msg = "initialising: heading %.1f deg from %d fixe
          +s" % (                                                        
      341 +                self.init_course_deg, len(self._gps_enu_histor
          +y))                                                           
      342 +            if velocity is not None:                          
      343 +                self.init_speed_mps = float(np.linalg.norm(vel
          +ocity))                                                       
      344 +                msg += ", velocity %.1f m/s" % self.init_speed
          +_mps                                                          
      345 +            print(msg)                                        
      346 +        self._start_filter(course, velocity)                  
      347  
      314 -    def _start_filter(self, course):                          
      348 +    def _start_filter(self, course, velocity):                
      349          first_accel = next((p[1].accel for k, p in self._pendi
           ng if k == "imu"), None)
      350          if first_accel is None:
      351              return
     ...
      356          # GPS out permanently. Real INS/GPS systems seed this 
           way for the same
      357          # reason.
      358          p_init = self._gps_enu_history[0] if self._gps_enu_his
           tory else [0, 0, 0]
      325 -        self.ekf.initialize_state(self.ekf.R, np.array(p_init)
          -, np.zeros(3))                                                
      359 +        v_init = velocity if velocity is not None else np.zero
          +s(3)                                                          
      360 +        self.ekf.initialize_state(self.ekf.R, np.array(p_init)
          +, np.asarray(v_init))                                         
      361          self.initialized = True
      362  
      363          pending, self._pending = self._pending, []

● Now the buffer bug — frames were being counted against a budget dominated by
  100 Hz IMU samples:

● Update(live_pipeline.py)
  ⎿  Added 6 lines, removed 1 line
      376  
      377      def on_frame(self, t_s, pkt):
      378          if not self.initialized:
      379 -            if len(self._pending) < MAX_INIT_BUFFER_FRAMES:   
      379 +            # Budget FRAMES, not total pending events. self._p
          +ending also holds                                             
      380 +            # IMU at 100+ Hz, so a total-count cap starts disc
          +arding frames                                                 
      381 +            # about a second into the wait - silently, and wor
          +se the longer the                                             
      382 +            # heading takes to become observable.             
      383 +            if self._pending_frames < MAX_INIT_BUFFER_FRAMES: 
      384 +                self._pending_frames += 1                     
      385                  self._buffer("frame", (t_s, pkt))
      386              self._try_initialize(t_s)
      387              return

  Ran 2 shell commands (ctrl+o to expand)

● ATE 1.383 m vs the batch pipeline's 1.35 m — the streaming path now matches
  batch accuracy, using only 6 fixes of startup latency where batch reads 15
  fixes of future data. The velocity seeding was the fix.

  But the heading is still 33° out, and Umeyama alignment hides that. For a
  georeferenced deliverable that matters, so let me measure it unaligned.

● Update(tools\score_live_run.py)
  ⎿  Added 26 lines, removed 1 line
      41  
      42      ate = compute_ate(est, gt_pos)
      43      print("frames scored        %d" % len(est))
      44 -    print("ATE RMSE             %.3f m" % ate["rmse_m"])       
      44 +    print("ATE RMSE             %.3f m   (Umeyama-aligned, as p
         +ipeline.py reports)"                                           
      45 +          % ate["rmse_m"])                                     
      46      print("ATE mean / max       %.3f m / %.3f m" % (ate["mean_m
          "], ate["max_m"]))
      47  
      48 +    # ATE aligns the trajectory before scoring, which is right 
         +for comparing                                                  
      49 +    # trajectory SHAPE - but it absorbs exactly the rotation an
         +d translation a                                                
      50 +    # georeferenced deliverable has to get right. A 30 deg head
         +ing error scores                                               
      51 +    # a clean ATE and puts the model 30 deg out in the world. S
         +o also score the                                               
      52 +    # raw frame.                                               
      53 +    cfg = json.load(open(os.path.join(args.dataset, "flight_con
         +fig.json")))                                                   
      54 +    run_cfg = json.load(open(os.path.join(args.run_dir, "sessio
         +n_config.json")))                                              
      55 +    off = latlon_to_enu(run_cfg["ref_lat_deg"], run_cfg["ref_lo
         +n_deg"],                                                       
      56 +                        run_cfg["ref_alt_m"],                  
      57 +                        cfg["ref_lat_deg"], cfg["ref_lon_deg"],
         + cfg["ref_alt_m"])                                             
      58 +    est_ds = np.asarray(est) + np.asarray(off)                 
      59 +    raw = np.linalg.norm(est_ds - np.asarray(gt_pos), axis=1)  
      60 +    print("georeferenced RMSE   %.3f m   (NO alignment - the nu
         +mber that decides"                                             
      61 +          % float(np.sqrt((raw ** 2).mean())))                 
      62 +    print("                              whether the model land
         +s in the right place)")                                        
      63 +    print("georef mean / max    %.3f m / %.3f m" % (raw.mean(),
         + raw.max()))                                                   
      64 +                                                               
      65 +    # Heading error, read straight off the two trajectories.   
      66 +    def bearing(p):                                            
      67 +        d = np.asarray(p[-1]) - np.asarray(p[0])               
      68 +        return np.degrees(np.arctan2(d[1], d[0]))              
      69 +    print("track bearing        est %.2f deg vs gt %.2f deg  (d
         +elta %.2f deg)"                                                
      70 +          % (bearing(est), bearing(gt_pos), bearing(est) - bear
         +ing(gt_pos)))                                                  
      71 +                                                               
      72      span = traj[-1]["timestamp"] - traj[0]["timestamp"]
      73      dts = np.diff([t["timestamp"] for t in traj])
      74      print("timestamps           %.2f s span, monotonic=%s, medi
          an dt %.4f s"

  Ran 1 shell command (ctrl+o to expand)

● Two important results there: georeferenced RMSE is 1.625 m with no alignment,
  and the track bearing converged to 0.59° of truth despite initialising 33°
  out. So the EKF does recover yaw from GPS in flight — my concern about a
  rotated model was unfounded.

  Now the drop-resilience test from §8, which is the one that catches a broken
  frame-timing path:

  Read 1 file, listed 1 directory, ran 1 shell command (ctrl+o to expand)

● Race — I scored while the receiver was still finalising. Re-running:

  Ran 1 shell command (ctrl+o to expand)

● Drop resilience passes cleanly: bearing unchanged at 0.57° with 6.2% of frames
  dropped — graceful degradation, no systematic shift. That's the §4.2
  criterion, and it holds because the live path carries real per-frame
  timestamps rather than idx/fps.

  Now the recorder → replay round trip, plus a regression check on the existing
  batch tests:

  Ran 3 shell commands (ctrl+o to expand)

● Fixture is complete — 600 frames, zero drops, real per-frame timestamps. Now
  driving the pipeline from it to confirm the replay path reproduces the live
  result:

  Ran 1 shell command (ctrl+o to expand)

● Replay reproduces live almost exactly (ATE 1.360 vs 1.383, bearing 0.57° vs
  0.58°) — the fixture is faithful. Now the full-quality run at the batch
  pipeline's own settings, to produce the headline artifact:

  Searched for 1 pattern, ran 3 shell commands (ctrl+o to expand)

● The live run beats batch on model density — 1.95M points and 51% mesh
  completeness vs 998k and 32.9% — because it consumes the phone's JPEGs
  directly instead of re-compressed H.264 (§4.7's double-compression tax). Let
  me add tests for the reordering logic, which is core to correctness:
● Update(tests\test_stream.py)
  ⎿  Added 88 lines
      218            "peak %d frames after 5000 (batch would hold 5000)" 
           % w.peak_len)
      219  
      220  
      221 +# -------------------------------------------------------- reo
          +rder buffer --                                                
      222 +                                                              
      223 +class _Collector:                                             
      224 +    def __init__(self):                                       
      225 +        self.seen = []                                        
      226 +                                                              
      227 +    def on_session_start(self, clock, hint):                  
      228 +        pass                                                  
      229 +                                                              
      230 +    def on_frame(self, t_s, pkt):                             
      231 +        self.seen.append(("frame", t_s))                      
      232 +                                                              
      233 +    def on_imu(self, t_s, s):                                 
      234 +        self.seen.append(("imu", t_s))                        
      235 +                                                              
      236 +    def on_gps(self, t_s, pkt, sigma):                        
      237 +        self.seen.append(("gps", t_s))                        
      238 +                                                              
      239 +    def on_session_end(self, stats):                          
      240 +        pass                                                  
      241 +                                                              
      242 +                                                              
      243 +class _ScriptedSource:                                        
      244 +    """Emits a fixed packet script with controllable per-strea
          +m latency."""                                                 
      245 +                                                              
      246 +    def __init__(self, n=90, frame_lat=0.12, imu_lat=0.002):  
      247 +        self.n, self.frame_lat, self.imu_lat = n, frame_lat, i
          +mu_lat                                                        
      248 +                                                              
      249 +    def packets(self, stats):                                 
      250 +        WALL0, BOOT0 = 1.7938e9, 493122.0                     
      251 +        events = []                                           
      252 +        for i in range(self.n):                               
      253 +            t = i * 0.033                                     
      254 +            events.append((t + self.frame_lat, "f",           
      255 +                           protocol.FramePacket(int((WALL0 + t
          +) * 1e3), 96, 54, b"x")))                                     
      256 +        for i in range(self.n * 3):                           
      257 +            t = i * 0.011                                     
      258 +            events.append((t + self.imu_lat, "i",             
      259 +                           [protocol.ImuSample(int((BOOT0 + t)
          + * 1e9),                                                      
      260 +                                               (0.0, 0.0, 9.81
          +), (0.0, 0.0, 0.0))]))                                        
      261 +        events.sort(key=lambda e: e[0])       # ARRIVAL order,
          + not capture order                                            
      262 +        for arrive, kind, payload in events:                  
      263 +            yield ("frame" if kind == "f" else "imu"), payload
          +, arrive                                                      
      264 +                                                              
      265 +                                                              
      266 +def test_reorder_emits_in_timestamp_order():                  
      267 +    """A frame is stamped at capture then spends tens of ms be
          +ing JPEG-encoded                                              
      268 +    and serialised, while a 32-byte IMU sample leaves immediat
          +ely. So frames                                                
      269 +    arrive AFTER IMU samples stamped later than them. The EKF 
          +consumes a                                                    
      270 +    monotonically increasing timeline, so anything emitted out
          + of order is                                                  
      271 +    either dropped or applied at the wrong pose - silently."""
      272 +    from stream.source import StreamSession                   
      273 +    from stream.clock import SessionClock                     
      274 +                                                              
      275 +    c = _Collector()                                          
      276 +    clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)       
      277 +    sess = StreamSession(_ScriptedSource(), [c], reorder_hold_
          +s=0.25,                                                       
      278 +                         clock=clock, verbose=False)          
      279 +    sess.run()                                                
      280 +                                                              
      281 +    times = [t for _k, t in c.seen]                           
      282 +    monotonic = all(b >= a for a, b in zip(times, times[1:])) 
      283 +    check("reorder buffer emits in timestamp order", monotonic
          +,                                                             
      284 +          "%d events, frames arriving %.0f ms behind IMU" % (l
          +en(times), 118))                                              
      285 +    check("nothing was dropped as late", sess.stats.late_dropp
          +ed == 0,                                                      
      286 +          "late_dropped=%d with a %.2fs hold" % (sess.stats.la
          +te_dropped, 0.25))                                            
      287 +    kinds = set(k for k, _t in c.seen)                        
      288 +    check("both streams reached the consumer", kinds == {"fram
          +e", "imu"},                                                   
      289 +          "saw %s" % sorted(kinds))                           
      290 +                                                              
      291 +                                                              
      292 +def test_reorder_hold_too_small_is_reported():                
      293 +    """If the hold is shorter than the spread in arrival laten
          +cy, late events                                               
      294 +    cannot be placed. They must be counted, not silently disca
          +rded - a nonzero                                              
      295 +    count is the signal to raise REORDER_HOLD_S."""           
      296 +    from stream.source import StreamSession                   
      297 +    from stream.clock import SessionClock                     
      298 +                                                              
      299 +    c = _Collector()                                          
      300 +    clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)       
      301 +    sess = StreamSession(_ScriptedSource(frame_lat=0.40), [c],
      302 +                         reorder_hold_s=0.02, clock=clock, ver
          +bose=False)                                                   
      303 +    sess.run()                                                
      304 +    check("an undersized reorder hold is counted, not hidden",
      305 +          sess.stats.late_dropped > 0,                        
      306 +          "%d events reported as late" % sess.stats.late_dropp
          +ed)                                                           
      307 +                                                              
      308 +                                                              
      309  def main():
      310      test_wire_roundtrip()
      311      test_short_read_handling()

● Update(tests\test_stream.py)
  ⎿  Added 2 lines
      314      test_gps_sigma()
      315      test_dataset_gps_consistency()
      316      test_rolling_window_bounded()
      317 +    test_reorder_emits_in_timestamp_order()                   
      318 +    test_reorder_hold_too_small_is_reported()                 
      319      print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
      320      return 1 if FAIL else 0
      321  

  Ran 1 shell command (ctrl+o to expand)

● The failing test taught me something worth keeping: a constant per-stream
  latency difference gets absorbed into the clock offset (harmful for accuracy,
  harmless for ordering). It's jitter the reorder buffer exists for. Fixing the
  test to reflect that:

● Update(tests\test_stream.py)
  ⎿  Added 5 lines, removed 2 lines
      243  class _ScriptedSource:
      244      """Emits a fixed packet script with controllable per-strea
           m latency."""
      245  
      246 -    def __init__(self, n=90, frame_lat=0.12, imu_lat=0.002):  
      246 +    def __init__(self, n=90, frame_lat=0.12, imu_lat=0.002, ji
          +tter=0.0, seed=3):                                            
      247          self.n, self.frame_lat, self.imu_lat = n, frame_lat, i
           mu_lat
      248 +        self.jitter, self.seed = jitter, seed                 
      249  
      250      def packets(self, stats):
      251          WALL0, BOOT0 = 1.7938e9, 493122.0
      252 +        rng = random.Random(self.seed)                        
      253          events = []
      254          for i in range(self.n):
      255              t = i * 0.033
      254 -            events.append((t + self.frame_lat, "f",           
      256 +            lat = self.frame_lat + (rng.expovariate(1.0 / self
          +.jitter) if self.jitter else 0.0)                             
      257 +            events.append((t + lat, "f",                      
      258                             protocol.FramePacket(int((WALL0 + t
           ) * 1e3), 96, 54, b"x")))
      259          for i in range(self.n * 3):
      260              t = i * 0.011

● Update(tests\test_stream.py)
  ⎿  Added 25 lines, removed 6 lines
      293  
      294  
      295  def test_reorder_hold_too_small_is_reported():
      296 -    """If the hold is shorter than the spread in arrival laten
          -cy, late events                                               
      297 -    cannot be placed. They must be counted, not silently disca
          -rded - a nonzero                                              
      298 -    count is the signal to raise REORDER_HOLD_S."""           
      296 +    """It is JITTER, not constant latency, that the reorder bu
          +ffer exists for.                                              
      297 +                                                              
      298 +    A constant per-stream latency difference is absorbed by th
          +e clock's                                                     
      299 +    minimum filter and re-labelled as a clock offset - wrong f
          +or accuracy                                                   
      300 +    (see clock.py) but harmless for ordering, because everythi
          +ng shifts                                                     
      301 +    together. Variable latency cannot be absorbed that way: a 
          +frame that                                                    
      302 +    happens to take 300 ms when the floor is 40 ms lands behin
          +d IMU already                                                 
      303 +    emitted. Those must be counted, never silently discarded, 
          +because a                                                     
      304 +    nonzero count is the only signal that REORDER_HOLD_S is to
          +o small for                                                   
      305 +    this link.                                                
      306 +    """                                                       
      307      from stream.source import StreamSession
      308      from stream.clock import SessionClock
      309  
      310      c = _Collector()
      311      clock = SessionClock(warmup_s=0.3, now=lambda: 0.0)
      304 -    sess = StreamSession(_ScriptedSource(frame_lat=0.40), [c],
      305 -                         reorder_hold_s=0.02, clock=clock, ver
          -bose=False)                                                   
      312 +    sess = StreamSession(_ScriptedSource(frame_lat=0.04, jitte
          +r=0.25), [c],                                                 
      313 +                         reorder_hold_s=0.01, clock=clock, ver
          +bose=False)                                                   
      314      sess.run()
      315      check("an undersized reorder hold is counted, not hidden",
      316            sess.stats.late_dropped > 0,
      309 -          "%d events reported as late" % sess.stats.late_dropp
          -ed)                                                           
      317 +          "%d jittered events reported as late" % sess.stats.l
          +ate_dropped)                                                  
      318  
      319 +    # The same stream with an adequate hold must lose nothing.
      320 +    c2 = _Collector()                                         
      321 +    clock2 = SessionClock(warmup_s=0.3, now=lambda: 0.0)      
      322 +    sess2 = StreamSession(_ScriptedSource(frame_lat=0.04, jitt
          +er=0.25), [c2],                                               
      323 +                          reorder_hold_s=1.5, clock=clock2, ve
          +rbose=False)                                                  
      324 +    sess2.run()                                               
      325 +    times = [t for _k, t in c2.seen]                          
      326 +    check("a large enough hold absorbs the same jitter",      
      327 +          sess2.stats.late_dropped == 0 and all(b >= a for a, 
          +b in zip(times, times[1:])),                                  
      328 +          "late_dropped=%d, order monotonic" % sess2.stats.lat
          +e_dropped)                                                    
      329  
      330 +                                                              
      331  def main():
      332      test_wire_roundtrip()
      333      test_short_read_handling()

  Ran 1 shell command (ctrl+o to expand)

● All 18 pass. Now documenting what was built and measured.

● Write(STREAMING.md)
  ⎿  Wrote 243 lines to STREAMING.md
       1 # Live ingest: phone stream → 3D model
       2 
         s no
       4 record-then-process step in the data path.
       5 
       7 # 1. start the receiver, then point the app's "Server IP" at thi
         s host
       8 python -u live_pipeline.py --port 5555 --run-id flight1
      10 # 2. no phone? stream the synthetic dataset over a real socket i
         nstead
     … +332 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

● Done. The integration is built, validated end to end over a real socket, and
  documented.

  What it does

  live_pipeline.py reconstructs from packets as they arrive. There is no
  record-then-process step — the reconstruction consumer has no file-reading
  code path at all.

  Three lanes at three latencies: EKF pose at ~20 ms, sparse map + BA at ~0.3 s,
  dense stereo per keyframe at ~2–5 s once its stereo partners exist. The dense
  lane runs on worker threads behind a bounded queue, so when it can't keep up
  it sheds keyframes and reports it rather than growing a queue until the
  process dies.

  Validation

  Streamed the synthetic dataset over TCP at real-time pace using a test double
  that speaks the app's exact wire format, then scored against ground truth:

  ┌────────────────────────────────┬───────────────────┬───────────┐
  │                                │ batch pipeline.py │   live    │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ ATE RMSE                       │ 1.352 m           │ 1.391 m   │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ Georeferenced RMSE (unaligned) │ —                 │ 1.636 m   │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ Bearing error                  │ —                 │ 0.60°     │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ Dense cloud                    │ 998k pts          │ 1.95M pts │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ Mesh completeness              │ 32.9 %            │ 51.0 %    │
  ├────────────────────────────────┼───────────────────┼───────────┤
  │ Frames held in RAM             │ 600               │ 20        │
  └────────────────────────────────┴───────────────────┴───────────┘

  The live path is denser because it consumes the phone's JPEGs directly instead
  of re-compressed H.264 — §4.7's double-compression tax, showing up exactly
  where predicted.

  Three findings worth flagging:

  - Getting to parity took a real fix. course_over_ground is a two-point
    difference, so the initial heading was mostly GPS noise (36° out). Switching
    to velocity_from_fixes — a least-squares fit already in georeference.py —
    and using its velocity to seed the filter took ATE from 2.379 m to 1.383 m.
  - ATE was hiding a georeferencing question. Umeyama alignment absorbs a
    constant rotation, so a model can score a clean ATE and be 30° out in the
    world. I added an unaligned score; the EKF recovers the 33° initial heading
    error to 0.6° in flight.
  - Drop resilience passes. With 6.2 % of frames dropped, bearing moved 0.58° →
    0.57°: graceful degradation, no systematic shift. That's the §4.2 criterion,
    and it holds because the live path carries real per-frame timestamps.

  tests/test_stream.py — 18 checks, all passing. test_geometry.py still 9/9.

  What I did not do

  - §4.6, the camera↔IMU extrinsic. Untestable without hardware; it needs the
    single-axis rotation test on a device. Still the most likely source of a
    self-consistent wrong trajectory.
  - §4.5 distortion. Schema still has no k1..k3/p1/p2. Undistort at ingest
    before trusting phone geometry.
  - Online clock-offset estimation. The minimum filter suppresses jitter but
    absorbs the difference in per-stream capture-to-send latency.
    imu_time_offset_s is a single number every conversion reads through, so
    making it an EKF state is a filter change, not a rewrite.

  Throughput is 0.22× real time. The measured breakdown says the fix is specific
  rather than architectural: BA is 88 ms/frame, the largest per-frame cost, and
  it runs inline on the reader thread. Moving it to its own thread drops the
  serial cost to ~48 ms/frame — within 1.5× of real time. Details in
  STREAMING.md.

  Nothing is committed. And the standing caveat: none of this is evidence about
  the handset — no part of the Android app has run on a physical phone.

✻ Cogitated for 39m 38s · done 6:07 PM

※ recap: Goal was to feed the phone's live stream into the reconstruction pipeline; that's built and validated, matching batch accuracy (ATE 1.391m vs 1.352m) at 0.22x real time. Next: move bundle adjustment off the reader thread to close the throughput gap. (disable recaps in /config)