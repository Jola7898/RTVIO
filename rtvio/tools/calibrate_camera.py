#!/usr/bin/env python3
"""
Checkerboard camera calibration -> camera_intrinsics.json, at whatever
resolution the input images actually are.

The shipped data/camera_intrinsics.json is a nominal synthetic pinhole and is
only a fallback (the phone now sends its own real intrinsics automatically -
see docs/CAMERA_INTRINSICS_INTEGRATION.md); this tool produces a real OpenCV
calibration for when that auto-discovery isn't available. Feed it images from
tools/capture_calibration_frames.py (or any set of checkerboard photos taken
at the same resolution and with the same lens/focus settings as the live
stream).

Usage:
    python tools/calibrate_camera.py --images data/calib_frames --board-cols 9 \\
        --board-rows 6 --square-size-mm 25.0

`--board-cols/--board-rows` count INNER corners (intersections), not squares -
a standard 10x7-square board has 9x6 inner corners. `--square-size-mm` is the
edge length of one physical square on your printed board; get it wrong and
every distance in the reconstruction scales by the same wrong factor.
"""
import argparse
import glob
import json
import os
import shutil
import sys

import cv2
import numpy as np

CORNER_SUBPIX_WIN = (11, 11)
CORNER_SUBPIX_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)

# Rules of thumb for the RMS reprojection error calibrateCamera returns, in
# pixels. Not a hard gate - printed on-screen so a bad calibration doesn't
# silently look as trustworthy as a good one.
RMS_GOOD_PX = 0.5
RMS_OK_PX = 1.0


def find_images(images_arg):
    if os.path.isdir(images_arg):
        paths = sorted(
            p for ext in ("*.jpg", "*.jpeg", "*.png")
            for p in glob.glob(os.path.join(images_arg, ext)))
    else:
        paths = sorted(glob.glob(images_arg))
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True,
                     help="directory of checkerboard images, or a glob pattern")
    ap.add_argument("--board-cols", type=int, default=9, help="inner corners, long side")
    ap.add_argument("--board-rows", type=int, default=6, help="inner corners, short side")
    ap.add_argument("--square-size-mm", type=float, required=True,
                     help="edge length of one physical square on the printed board")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default=os.path.join(repo_root, "data", "camera_intrinsics.json"))
    ap.add_argument("--no-distortion", action="store_true",
                     help="write only fx/fy/cx/cy (drop k1/k2/p1/p2/k3). Only do this if "
                          "something downstream can't yet handle the extra fields.")
    args = ap.parse_args()

    board_size = (args.board_cols, args.board_rows)
    paths = find_images(args.images)
    if not paths:
        sys.exit("no images found at %r" % args.images)

    # Object points are the same planar grid for every image, in board units
    # (mm) - z=0 because the board is flat. calibrateCamera solves the
    # per-image pose (rvec/tvec) that explains the observed pixel corners.
    objp = np.zeros((board_size[0] * board_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:board_size[0], 0:board_size[1]].T.reshape(-1, 2)
    objp *= args.square_size_mm

    objpoints, imgpoints = [], []
    image_size = None
    used, skipped = 0, 0

    for path in paths:
        img = cv2.imread(path)
        if img is None:
            print("skip %s: unreadable" % path)
            skipped += 1
            continue
        h, w = img.shape[:2]
        if image_size is None:
            image_size = (w, h)
        elif (w, h) != image_size:
            print("skip %s: %dx%d doesn't match the first image's %dx%d - "
                  "a calibration set must be one resolution"
                  % (path, w, h, image_size[0], image_size[1]))
            skipped += 1
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found, corners = cv2.findChessboardCorners(
            gray, board_size,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            print("skip %s: board not found" % path)
            skipped += 1
            continue

        corners = cv2.cornerSubPix(gray, corners, CORNER_SUBPIX_WIN, (-1, -1),
                                    CORNER_SUBPIX_CRITERIA)
        objpoints.append(objp)
        imgpoints.append(corners)
        used += 1

    print("%d images used, %d skipped" % (used, skipped))
    if used < 8:
        sys.exit("only %d usable images (need >= 8, more like 15-20 for a good fit "
                  "covering the frame including corners) - capture more views" % used)

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        objpoints, imgpoints, image_size, None, None)

    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]
    d = dist.ravel().tolist() + [0.0] * 5   # pad in case OpenCV returns <5 coeffs
    k1, k2, p1, p2, k3 = d[:5]

    print("\nRMS reprojection error: %.3f px" % rms, end="  ")
    if rms <= RMS_GOOD_PX:
        print("(good)")
    elif rms <= RMS_OK_PX:
        print("(OK, usable)")
    else:
        print("(HIGH - board flat? cornerSubPix converging on the right pattern? "
              "consider recapturing with better lighting/more angles)")
    print("fx=%.2f fy=%.2f cx=%.2f cy=%.2f  (image %dx%d, principal point offset "
          "from centre: %.1f, %.1f px)"
          % (fx, fy, cx, cy, image_size[0], image_size[1],
             cx - image_size[0] / 2, cy - image_size[1] / 2))
    print("distortion k1=%.5f k2=%.5f p1=%.5f p2=%.5f k3=%.5f" % (k1, k2, p1, p2, k3))

    out = {
        "fx": float(fx), "fy": float(fy), "cx": float(cx), "cy": float(cy),
        "width": int(image_size[0]), "height": int(image_size[1]),
    }
    if not args.no_distortion:
        out.update({"k1": float(k1), "k2": float(k2), "p1": float(p1),
                     "p2": float(p2), "k3": float(k3)})

    if os.path.exists(args.out):
        backup = args.out + ".bak"
        shutil.copy(args.out, backup)
        print("\nbacked up existing %s -> %s" % (args.out, backup))

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
