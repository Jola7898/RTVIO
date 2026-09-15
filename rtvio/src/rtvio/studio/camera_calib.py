"""
Checkerboard capture for calibrating the drone camera's lens, driven from
the Studio (Drone tab -> Camera calibration).

The drone's video thread offers every frame; a worker thread looks for the
board a few times a second on the newest one, so detection can never slow
the stream down. A detection only becomes a calibration view if the board
is somewhere meaningfully new - position, size or tilt - because twenty
copies of the same pose constrain the lens no better than one. Coverage of
the image is tracked on a 3x3 grid: distortion is strongest in the
corners, so views there matter most.

Views are solved at the camera's native stream resolution; the resulting
profile (camera_model) scales to whatever size the takes are recorded at.
Every accepted frame is also saved as a JPEG, so the same set can be
re-solved offline with tools/calibrate_camera.py --model auto.
"""
import math
import os
import threading
import time

import cv2
import numpy as np

from .. import camera_model

DETECT_EVERY_S = 0.25
DETECT_LONG_SIDE = 960          # detection on a downsized copy; corners refined at full size
TARGET_VIEWS = 25
MIN_VIEWS = 10
NOVELTY = 0.08                  # distance in (centre x, centre y, size, tilt x, tilt y) to count as new
SUBPIX_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)


def _describe(c, size, board):
    """Where the board is in the view: centre, apparent size, and
    perspective tilt from its opposite edges' length ratio."""
    cols, rows = board
    g = c.reshape(rows, cols, 2)
    w, h = size
    centre = g.reshape(-1, 2).mean(axis=0) / (w, h)
    hull = cv2.convexHull(g.reshape(-1, 1, 2).astype(np.float32))
    area = math.sqrt(max(cv2.contourArea(hull), 1.0) / (w * h))
    edge = lambda a, b: float(np.linalg.norm(a - b))
    top, bottom = edge(g[0, 0], g[0, -1]), edge(g[-1, 0], g[-1, -1])
    left, right = edge(g[0, 0], g[-1, 0]), edge(g[0, -1], g[-1, -1])
    tx = (top - bottom) / max(top + bottom, 1e-6)
    ty = (left - right) / max(left + right, 1e-6)
    return np.array([centre[0], centre[1], area, 2 * tx, 2 * ty])


class CalibrationSession:
    def __init__(self, cols, rows, save_dir):
        self.board = (int(cols), int(rows))
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
        self.views = []                 # (N,2) float64 corners at full resolution
        self._descs = []
        self.size = None
        self.coverage = np.zeros((3, 3), int)
        self.last_seen = None           # monotonic time of the last detection
        self.last_corners = None
        self.status = "show the checkerboard to the camera"
        self.active = True
        self.started = time.time()
        self._slot = None
        self._gen = 0
        self._cond = threading.Condition()
        threading.Thread(target=self._worker, args=(0,), daemon=True, name="drone-calib").start()

    # ---------------------------------------------------------- frames --

    def offer(self, frame):
        """Video thread: hand over the newest frame. Never blocks."""
        if self.active:
            with self._cond:
                self._slot = frame
                self._cond.notify()

    def stop(self):
        with self._cond:
            self.active = False
            self._cond.notify()

    def resume(self):
        with self._cond:
            if self.active:
                return
            self.active = True
            self._gen += 1
            gen = self._gen
        threading.Thread(target=self._worker, args=(gen,), daemon=True, name="drone-calib").start()

    def _worker(self, gen):
        last = 0.0
        while True:
            wait = DETECT_EVERY_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            with self._cond:
                while self.active and self._slot is None and gen == self._gen:
                    self._cond.wait(0.5)
                if not self.active or gen != self._gen:
                    return
                frame, self._slot = self._slot, None
            last = time.monotonic()
            try:
                self.detect(frame)
            except cv2.error as e:
                self.status = "detection error: %s" % e

    def detect(self, frame):
        """Looks for the board in one frame; keeps it as a view if it is new.
        Returns True if a view was added."""
        h, w = frame.shape[:2]
        if self.size is None:
            self.size = (w, h)
        elif (w, h) != self.size:
            self.status = "the video resolution changed - start the capture again"
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        s = min(1.0, DETECT_LONG_SIDE / max(w, h))
        small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1.0 else gray
        found, c = cv2.findChessboardCorners(
            small, self.board,
            cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK)
        if not found:
            self.last_corners = None
            self.status = ("board not found - hold the whole board flat in view, %dx%d inner corners"
                           % self.board) if not self.views else "looking for the board (%d/%d views)" % (
                               len(self.views), TARGET_VIEWS)
            return False
        c = ((c.reshape(-1, 2) + 0.5) / s - 0.5).astype(np.float32)
        # Refinement window under half a square, or it can snap to the neighbouring corner.
        g = c.reshape(self.board[1], self.board[0], 2)
        spacing = min(np.linalg.norm(np.diff(g, axis=1), axis=2).min(),
                      np.linalg.norm(np.diff(g, axis=0), axis=2).min())
        win = int(max(2, min(11, spacing * 0.4)))
        c = cv2.cornerSubPix(gray, c.reshape(-1, 1, 2), (win, win), (-1, -1), SUBPIX_CRITERIA).reshape(-1, 2)
        self.last_seen = time.monotonic()
        self.last_corners = c
        d = _describe(c, self.size, self.board)
        if self._descs and min(float(np.linalg.norm(d - e)) for e in self._descs) < NOVELTY:
            self.status = ("board found - move or tilt it somewhere new (%d/%d views)"
                           % (len(self.views), TARGET_VIEWS))
            return False
        self.views.append(c.astype(np.float64))
        self._descs.append(d)
        cells = {(min(2, int(3 * x / w)), min(2, int(3 * y / h))) for x, y in c}
        for cx, cy in cells:
            self.coverage[cy, cx] += 1
        cv2.imwrite(os.path.join(self.save_dir, "view_%02d.jpg" % len(self.views)), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        if len(self.views) >= TARGET_VIEWS:
            self.active = False
            self.status = "%d views captured - press Calibrate" % len(self.views)
        else:
            self.status = "view %d/%d captured" % (len(self.views), TARGET_VIEWS)
        return True

    # ---------------------------------------------------------- result --

    def solve(self):
        if len(self.views) < MIN_VIEWS:
            raise ValueError("need at least %d views, have %d" % (MIN_VIEWS, len(self.views)))
        prof = camera_model.calibrate_best(self.views, self.board, self.size)
        prof["calibrated"] = time.strftime("%Y-%m-%d %H:%M")
        prof["source"] = "checkerboard %dx%d, %d views (RTVIO Studio)" % (self.board + (prof["views"],))
        prof["views_dir"] = self.save_dir
        return prof

    def summary(self):
        now = time.monotonic()
        return {
            "active": self.active,
            "board": list(self.board),
            "views": len(self.views),
            "target": TARGET_VIEWS,
            "min_views": MIN_VIEWS,
            "coverage": self.coverage.tolist(),
            "seen_s_ago": round(now - self.last_seen, 1) if self.last_seen else None,
            "status": self.status,
            "size": list(self.size) if self.size else None,
            "dir": self.save_dir,
        }
