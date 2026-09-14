"""
Turn a folder of frames into an rtvio.studio session, so it shows up in the
web UI and can be reconstructed like a live take.

    python tools/import_frames_as_session.py <frames_dir> [--name NAME] [--fps 30]

Understands mock_receiver.py --save-frames names (frame_NNNNNN_<epoch_ms>.jpg),
whose embedded capture times become frame_timestamps.json; any other image
names are taken in sorted order at --fps. Files are hard-linked when the
source is on the same drive (no copy), copied otherwise.
"""
import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SESSIONS = os.path.join(HERE, "..", "data", "sessions")
MOCK_NAME = re.compile(r"frame_(\d+)_(\d{10,})\.jpe?g$", re.IGNORECASE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frames_dir")
    ap.add_argument("--name", default=None, help="session id (default: imported-<folder name>)")
    ap.add_argument("--fps", type=float, default=30.0, help="frame rate for names without timestamps")
    ap.add_argument("--sessions-root", default=DEFAULT_SESSIONS)
    args = ap.parse_args()

    names = sorted(f for f in os.listdir(args.frames_dir)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    if len(names) < 2:
        sys.exit("need at least 2 images in %s" % args.frames_dir)
    stamped = [MOCK_NAME.search(f) for f in names]
    if all(stamped):
        order = sorted(range(len(names)), key=lambda i: int(stamped[i].group(1)))
        ms = [int(stamped[i].group(2)) for i in order]
        times = [round((m - ms[0]) / 1e3, 6) for m in ms]
        names = [names[i] for i in order]
    else:
        times = [round(i / args.fps, 6) for i in range(len(names))]

    sid = args.name or "imported-" + os.path.basename(os.path.abspath(args.frames_dir))
    out = os.path.abspath(os.path.join(args.sessions_root, sid))
    if os.path.exists(out):
        sys.exit("%s already exists" % out)
    os.makedirs(os.path.join(out, "frames"))
    for i, name in enumerate(names):
        src = os.path.join(args.frames_dir, name)
        dst = os.path.join(out, "frames", "%06d%s" % (i, ".jpg"))
        if not name.lower().endswith((".jpg", ".jpeg")):
            import cv2
            cv2.imwrite(dst, cv2.imread(src), [cv2.IMWRITE_JPEG_QUALITY, 95])
            continue
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)

    span = times[-1] - times[0]
    with open(os.path.join(out, "frame_timestamps.json"), "w") as f:
        json.dump(times, f)
    with open(os.path.join(out, "gps_data.json"), "w") as f:
        json.dump([], f)
    import cv2
    h, w = cv2.imread(os.path.join(out, "frames", "000000.jpg")).shape[:2]
    meta = {"id": sid, "origin": "imported", "reason": "imported from %s" % os.path.abspath(args.frames_dir),
            "frames_received": len(names), "frames_reported_sent": None, "complete": True,
            "resolution": [w, h], "duration_s": round(span, 3),
            "fps_median": None, "fps_mean": round((len(names) - 1) / span, 2) if span > 0 else 0.0,
            "frame_gaps": 0, "gps_fixes": 0, "imu_samples": 0}
    with open(os.path.join(out, "session_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("session %s: %d frames, %.1f s, %dx%d -> %s" % (sid, len(names), span, w, h, out))


if __name__ == "__main__":
    main()
