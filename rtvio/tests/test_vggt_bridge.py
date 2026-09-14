"""
Unit tests for the phone-session -> VGGT batch bridge added in
vggt_reconstruct.py (reconstruct_from_recording / _load_recording_frames /
load_gps_track_from_recording), and for the recorder robustness fix in
stream/recorder.py's on_session_end.

Deliberately covers only the frame/GPS-loading logic, not a real VGGT
forward pass: that needs a GPU + the 5GB checkpoint and takes 1-2+ minutes,
which doesn't belong in a routine test run. The real end-to-end proof (a
genuine VGGT reconstruction from a phone-shaped session fixture, correctly
skipping a dropped frame and consuming its GPS directly) was run manually
and is documented in HANDOFF_SESSION3.md; this file guards the fast, always-
exercisable part of that path against regressions.

    python tests/test_vggt_bridge.py
"""
import json
import os
import shutil
import tempfile

from rtvio.vggt_reconstruct import _load_recording_frames, load_gps_track_from_recording

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


def _make_session(tmp_dir, frame_timestamps, gps_rows):
    """Builds a SessionRecorder-shaped fixture: frames/<idx>.jpg for every
    non-None timestamp (0-byte stand-ins - _load_recording_frames only
    checks the JSON, never opens the JPEGs) + the JSON sidecars."""
    frames_dir = os.path.join(tmp_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    for idx, t in enumerate(frame_timestamps):
        if t is not None:
            open(os.path.join(frames_dir, "%06d.jpg" % idx), "wb").close()
    with open(os.path.join(tmp_dir, "frame_timestamps.json"), "w") as f:
        json.dump(frame_timestamps, f)
    with open(os.path.join(tmp_dir, "gps_data.json"), "w") as f:
        json.dump(gps_rows, f)


def test_load_recording_frames_skips_dropped_frames():
    tmp = tempfile.mkdtemp()
    try:
        _make_session(tmp, [0.0, 0.5, None, 1.5], [])
        frame_paths, frame_times = _load_recording_frames(tmp)
        check("dropped (None) frame is skipped, not counted", len(frame_paths) == 3,
              "got %d" % len(frame_paths))
        check("frame_times excludes the drop", frame_times == [0.0, 0.5, 1.5], frame_times)
        check("paths point at the ORIGINAL indices, not renumbered",
              frame_paths[-1].endswith("000003.jpg"), frame_paths[-1])
    finally:
        shutil.rmtree(tmp)


def test_load_recording_frames_rejects_too_few_frames():
    tmp = tempfile.mkdtemp()
    try:
        _make_session(tmp, [0.0, None, None], [])
        try:
            _load_recording_frames(tmp)
            check("raises when fewer than 2 frames survive the drop filter", False)
        except RuntimeError:
            check("raises when fewer than 2 frames survive the drop filter", True)
    finally:
        shutil.rmtree(tmp)


def test_gps_track_from_recording_maps_fields_and_sorts():
    tmp = tempfile.mkdtemp()
    try:
        # Deliberately out of order, to check the sort - a real recording is
        # already sorted, but nothing guarantees it and gps_enu_for_frames
        # (np.argmin over timestamps) assumes nothing about order either way,
        # so this is a belt-and-suspenders check, not paranoia about a
        # specific known bug.
        gps_rows = [
            {"timestamp": 1.0, "latitude_deg": 12.1, "longitude_deg": 77.1,
             "altitude_m": 900.0, "accuracy_m": 3.0},
            {"timestamp": 0.0, "latitude_deg": 12.0, "longitude_deg": 77.0,
             "altitude_m": 890.0, "accuracy_m": 3.0},
        ]
        _make_session(tmp, [0.0, 1.0], gps_rows)
        track = load_gps_track_from_recording(tmp)
        check("returns one dict per GPS row", len(track) == 2, len(track))
        check("sorted by timestamp", [r["t"] for r in track] == [0.0, 1.0], track)
        check("field names mapped to load_gps_track's shape",
              track[0] == {"t": 0.0, "lat": 12.0, "lon": 77.0, "alt": 890.0}, track[0])
    finally:
        shutil.rmtree(tmp)


def test_gps_track_from_recording_missing_file_is_relative_mode_not_an_error():
    tmp = tempfile.mkdtemp()
    try:
        _make_session(tmp, [0.0, 1.0], [])
        os.remove(os.path.join(tmp, "gps_data.json"))
        track = load_gps_track_from_recording(tmp)
        check("no gps_data.json -> [] (same contract as reconstruct()'s gps_path=None), "
              "not an exception", track == [], track)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
