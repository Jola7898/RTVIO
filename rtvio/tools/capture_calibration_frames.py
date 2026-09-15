#!/usr/bin/env python3
"""
Capture checkerboard images from the live phone stream, at the phone's real
streaming resolution, for tools/calibrate_camera.py.

WHY THIS EXISTS

camera_intrinsics.json must describe the exact width/height the phone streams
(see docs/CAMERA_INTRINSICS_INTEGRATION.md for the auto-discovery path this
is a fallback for) - live_pipeline.py hard-fails rather than guess
if they don't match, because fx/fy/cx/cy are in pixels and a resolution
mismatch silently rescales the whole reconstruction. The phone's actual
output size is a CameraX ResolutionStrategy decision made on-device
(FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER against whatever the sensor
advertises) and is not guaranteed to be the 720p/1080p preset requested in
settings - e.g. a phone can hand back 1728x2304. So calibration images must
come from the same stream, at the same resolution, not a separate camera app.

This tool speaks the same wire protocol as live_pipeline.py (via
rtvio.stream.protocol / rtvio.stream.source.SocketPacketSource) but does none
of the reconstruction - it just watches for a checkerboard, saves
full-resolution JPEGs of the frames where one is clearly visible, and stops
once enough distinct views have been collected.

Usage:
    python tools/capture_calibration_frames.py --port 5555 --out data/calib_frames

Then walk the checkerboard through the frame at varied distances, angles and
positions (corners of the frame especially - that's where distortion is
worst and least visible if under-sampled), until it stops on its own.
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np

from rtvio.stream import protocol
from rtvio.stream.source import SocketPacketSource, SessionStats, KIND_FRAME

# Detection runs on a downsized copy purely for speed/responsiveness; the
# frame saved to disk is always the original full-resolution JPEG bytes as
# they came off the wire, because that's what calibrate_camera.py must find
# corners in at the resolution camera_intrinsics.json will describe.
DETECT_MAX_EDGE = 960


def find_board(img_bgr, board_size):
    h, w = img_bgr.shape[:2]
    scale = min(1.0, DETECT_MAX_EDGE / max(h, w))
    small = cv2.resize(img_bgr, (int(w * scale), int(h * scale))) if scale < 1.0 else img_bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    found, _ = cv2.findChessboardCorners(
        gray, board_size,
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_FAST_CHECK | cv2.CALIB_CB_NORMALIZE_IMAGE)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--out", default="data/calib_frames", help="directory to save accepted frames into")
    ap.add_argument("--board-cols", type=int, default=9,
                     help="inner corners along the checkerboard's long side")
    ap.add_argument("--board-rows", type=int, default=6,
                     help="inner corners along the checkerboard's short side")
    ap.add_argument("--target", type=int, default=20,
                     help="stop automatically after this many accepted frames")
    ap.add_argument("--min-interval-s", type=float, default=1.0,
                     help="minimum time between accepted frames, so holding the "
                          "board still doesn't fill the target with near-duplicates")
    args = ap.parse_args()

    board_size = (args.board_cols, args.board_rows)
    os.makedirs(args.out, exist_ok=True)

    stats = SessionStats()
    source = SocketPacketSource(port=args.port,
                                 on_connect=lambda addr: print("phone connected: %s:%d" % addr))

    saved = 0
    seen_wh = None
    last_saved_t = 0.0
    print("waiting for checkerboard (%dx%d inner corners)... Ctrl+C to stop early"
          % board_size)
    try:
        for kind, payload, t_recv in source.packets(stats):
            if kind != KIND_FRAME:
                continue
            if seen_wh is None:
                seen_wh = (payload.width, payload.height)
                print("phone is streaming %dx%d - this is the resolution "
                      "camera_intrinsics.json must end up describing" % seen_wh)
            elif (payload.width, payload.height) != seen_wh:
                print("WARNING: resolution changed mid-session (%dx%d -> %dx%d); "
                      "ignoring this frame" % (seen_wh[0], seen_wh[1],
                                                payload.width, payload.height))
                continue

            if t_recv - last_saved_t < args.min_interval_s:
                continue

            img = cv2.imdecode(np.frombuffer(payload.jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if not find_board(img, board_size):
                continue

            path = os.path.join(args.out, "calib_%03d.jpg" % saved)
            with open(path, "wb") as f:
                f.write(payload.jpeg)
            saved += 1
            last_saved_t = t_recv
            print("[%2d/%d] saved %s (board found)" % (saved, args.target, path))
            if saved >= args.target:
                print("target reached, stopping.")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    except (protocol.StreamClosed, OSError) as e:
        print("stream ended: %s" % e)

    if saved == 0:
        print("no frames saved - nothing to calibrate from. Common causes: the "
              "board wasn't held flat/steady/in-frame long enough, or "
              "--board-cols/--board-rows don't match the physical board.")
        sys.exit(1)

    print("\n%d calibration images saved to %s" % (saved, args.out))
    print("next: python tools/calibrate_camera.py --images %s --board-cols %d "
          "--board-rows %d --square-size-mm <YOUR BOARD'S SQUARE SIZE>"
          % (args.out, args.board_cols, args.board_rows))


if __name__ == "__main__":
    main()
