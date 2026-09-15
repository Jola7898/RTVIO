"""
Lens models for RTVIO: calibration profiles, field of view, checkerboard
calibration, and undistortion to an ideal pinhole camera.

VGGT assumes a pinhole camera. A wide-angle or fisheye lens - the iDronam
drone's camera bows every straight wall in its frames - cannot be described
by any single focal length, so VGGT settles on a compromise field of view
and the right angles in the scene come out opened up (a 90 degree corner
between walls reconstructed at ~150). The fix is to calibrate the lens once
and remap every frame to a pinhole image before VGGT sees it.

Profile format (JSON) - data/drone_camera.json, the camera_intrinsics.json a
session carries, and tools/calibrate_camera.py all use it:

    {"model": "fisheye" | "pinhole", "width": W, "height": H,
     "fx": .., "fy": .., "cx": .., "cy": ..,
     "k1", "k2", "k3", "k4"          fisheye: Kannala-Brandt, OpenCV's cv2.fisheye
     "k1", "k2", "p1", "p2", "k3"    pinhole: Brown-Conrady, cv2.calibrateCamera
     ...plus free-form provenance (rms_px, views, source, calibrated)}

A missing "model" means pinhole, so older camera_intrinsics.json files (the
phone's Camera2 values, calibrate_camera.py) read unchanged. Distortion
coefficients act on normalised camera coordinates, so a profile applies at
any resolution with the same aspect ratio: scaled() only touches fx/fy/cx/cy.
"""
import json
import math

import cv2
import numpy as np

FISHEYE_D = ("k1", "k2", "k3", "k4")
PINHOLE_D = ("k1", "k2", "p1", "p2", "k3")
MIN_VIEWS = 8


# --------------------------------------------------------------- profiles --

def normalize(raw):
    """A profile dict with every field present, or None if it has no K.
    Accepts the phone's IntrinsicsPacket names (fx_pix, ...) too."""
    if not isinstance(raw, dict):
        return None
    p = dict(raw)
    for k in ("fx", "fy", "cx", "cy"):
        if k not in p and k + "_pix" in p:
            p[k] = p[k + "_pix"]
    if not all(isinstance(p.get(k), (int, float)) for k in ("fx", "fy", "cx", "cy")):
        return None
    p["model"] = "fisheye" if p.get("model") == "fisheye" else "pinhole"
    for k in (FISHEYE_D if p["model"] == "fisheye" else PINHOLE_D):
        p[k] = float(p.get(k) or 0.0)
    return p


def load_profile(path):
    try:
        with open(path) as f:
            return normalize(json.load(f))
    except (OSError, ValueError, TypeError):
        return None


def matrices(p):
    K = np.array([[p["fx"], 0.0, p["cx"]], [0.0, p["fy"], p["cy"]], [0.0, 0.0, 1.0]])
    D = np.array([p[k] for k in (FISHEYE_D if p["model"] == "fisheye" else PINHOLE_D)], dtype=np.float64)
    return K, D


def has_distortion(p):
    return bool(np.any(np.abs(matrices(p)[1]) > 1e-9))


def scaled(p, width, height):
    """p at another resolution with the same aspect ratio. None if p has no
    size, or the aspect differs - that is a crop, which the profile does not
    describe."""
    w0, h0 = p.get("width"), p.get("height")
    if not w0 or not h0:
        return None
    sx, sy = width / w0, height / h0
    if abs(sx / sy - 1.0) > 0.01:
        return None
    q = dict(p, width=int(width), height=int(height))
    q["fx"], q["cx"] = p["fx"] * sx, (p["cx"] + 0.5) * sx - 0.5
    q["fy"], q["cy"] = p["fy"] * sy, (p["cy"] + 0.5) * sy - 0.5
    return q


# ------------------------------------------------------------------ rays --

def undistort_points(p, pts, P=None):
    """Pixels (N,2) of p's images -> (N,2): undistorted normalised
    coordinates, or pixels of the pinhole camera P (e.g. undistort_maps'
    K_new) when P is given."""
    K, D = matrices(p)
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    if p["model"] == "fisheye":
        out = cv2.fisheye.undistortPoints(pts, K, D, P=P)
    else:
        # More iterations than the default 5, which falls short near the
        # edges of a strongly distorted lens.
        crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-9)
        if hasattr(cv2, "undistortPointsIter"):          # OpenCV 4.x
            out = cv2.undistortPointsIter(pts, K, D, None, P, crit)
        else:                                            # OpenCV 5: undistortPoints takes criteria itself
            out = cv2.undistortPoints(pts, K, D, P=P, criteria=crit)
    return out.reshape(-1, 2)


def normalized_rays(p, pts):
    """Pixels (N,2) -> undistorted normalised coordinates (N,2), i.e. the
    ray (x, y, 1) each pixel sees."""
    return undistort_points(p, pts)


def fov_deg(p):
    """(horizontal, vertical, diagonal) field of view in degrees, edge to
    edge through the principal point: what the lens really covers, not what
    a pinhole with the same fx would. None without a size."""
    if not p.get("width") or not p.get("height"):
        return None
    w, h, cx, cy = p["width"], p["height"], p["cx"], p["cy"]
    pts = [(-0.5, cy), (w - 0.5, cy), (cx, -0.5), (cx, h - 0.5), (-0.5, -0.5), (w - 0.5, h - 0.5)]
    ang = [math.degrees(math.atan(math.hypot(x, y))) for x, y in normalized_rays(p, pts)]
    return ang[0] + ang[1], ang[2] + ang[3], ang[4] + ang[5]


def pinhole_fov_deg(K, width, height):
    return (math.degrees(2 * math.atan(width / 2.0 / K[0, 0])),
            math.degrees(2 * math.atan(height / 2.0 / K[1, 1])))


# ----------------------------------------------------------- undistortion --

def undistort_maps(p, balance=0.0):
    """cv2.remap tables turning p's images into an ideal pinhole camera of
    the same size -> (map1, map2, K_new). balance 0 keeps only pixels valid
    everywhere - no black border, the lens's outermost edge is cropped;
    1 keeps the whole field of view, with black corners and heavily
    stretched edges."""
    K, D = matrices(p)
    size = (int(p["width"]), int(p["height"]))
    if p["model"] == "fisheye":
        K_new = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(K, D, size, np.eye(3), balance=balance)
        m1, m2 = cv2.fisheye.initUndistortRectifyMap(K, D, np.eye(3), K_new, size, cv2.CV_16SC2)
    else:
        K_new, _roi = cv2.getOptimalNewCameraMatrix(K, D, size, balance, size)
        m1, m2 = cv2.initUndistortRectifyMap(K, D, np.eye(3), K_new, size, cv2.CV_16SC2)
    return m1, m2, K_new


def summary(p):
    """The numbers worth showing about a profile (Studio, reports)."""
    if p is None:
        return None
    out = {k: p.get(k) for k in ("model", "width", "height", "rms_px", "views", "calibrated", "source")}
    out.update(fx=round(p["fx"], 1), fy=round(p["fy"], 1))
    fov = fov_deg(p)
    if fov:
        out.update(hfov=round(fov[0], 1), vfov=round(fov[1], 1), dfov=round(fov[2], 1))
    if p.get("width") and has_distortion(p):
        _m1, _m2, K_new = undistort_maps(p)
        hf, vf = pinhole_fov_deg(K_new, p["width"], p["height"])
        out["undistorted"] = {"fx": round(float(K_new[0, 0]), 1), "hfov": round(hf, 1), "vfov": round(vf, 1)}
    return out


# ------------------------------------------------------------ calibration --

def board_points(cols, rows):
    """Inner-corner grid in board units (square = 1): the focal length and
    distortion do not depend on the physical square size, only the board
    poses would."""
    objp = np.zeros((rows * cols, 3), np.float64)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    return objp


def _rms(a, b):
    d = np.asarray(a, np.float64).reshape(-1, 2) - np.asarray(b, np.float64).reshape(-1, 2)
    return float(np.sqrt((d ** 2).sum(axis=1).mean()))


def _fisheye_flag(name):
    """cv2.fisheye.CALIB_* in OpenCV 4; OpenCV 5 moved them to the shared cv2.CALIB_*."""
    return getattr(cv2.fisheye, name) if hasattr(cv2.fisheye, name) else getattr(cv2, name)


def _solve(views, objp, size, model):
    n = len(views)
    if model == "fisheye":
        # (1, N, 3) / (1, N, 2): OpenCV 5's fisheye.calibrate rejects the
        # (N, 1, 3) layout calibrateCamera takes; 4.x accepts both.
        obj = [objp.reshape(1, -1, 3)] * n
        img = [v.reshape(1, -1, 2) for v in views]
        flags = _fisheye_flag("CALIB_RECOMPUTE_EXTRINSIC") | _fisheye_flag("CALIB_FIX_SKEW")
        crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-9)
        try:
            _r, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
                obj, img, size, np.zeros((3, 3)), np.zeros((4, 1)),
                flags=flags | _fisheye_flag("CALIB_CHECK_COND"), criteria=crit)
        except cv2.error:
            # CHECK_COND rejects near-degenerate views outright; the
            # per-view error pass in calibrate() drops them instead.
            _r, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
                obj, img, size, np.zeros((3, 3)), np.zeros((4, 1)), flags=flags, criteria=crit)
        errs = [_rms(cv2.fisheye.projectPoints(o, r, t, K, D)[0], v)
                for o, v, r, t in zip(obj, img, rvecs, tvecs)]
        names = FISHEYE_D
    else:
        obj = [objp.astype(np.float32)] * n
        img = [v.astype(np.float32).reshape(-1, 1, 2) for v in views]
        _r, K, D, rvecs, tvecs = cv2.calibrateCamera(obj, img, size, None, None)
        errs = [_rms(cv2.projectPoints(o, r, t, K, D)[0], v)
                for o, v, r, t in zip(obj, img, rvecs, tvecs)]
        names = PINHOLE_D
    D = D.ravel().tolist() + [0.0] * 5
    prof = {"model": model, "width": int(size[0]), "height": int(size[1]),
            "fx": float(K[0, 0]), "fy": float(K[1, 1]), "cx": float(K[0, 2]), "cy": float(K[1, 2])}
    prof.update({k: float(D[i]) for i, k in enumerate(names)})
    total = math.sqrt(sum(e * e for e in errs) / len(errs))
    prof["rms_px"] = round(total, 4)
    return prof, errs


def calibrate(img_points, board, size, model="fisheye"):
    """Checkerboard views -> profile. img_points: one (N,2) corner array per
    view in board order; board: (cols, rows) inner corners; size: (w, h).
    Views reprojecting far worse than the rest (a misdetected corner, a
    smeared frame) are dropped and the fit redone once."""
    objp = board_points(*board)
    views = [np.asarray(v, np.float64).reshape(-1, 2) for v in img_points]
    if len(views) < MIN_VIEWS:
        raise ValueError("need at least %d views, got %d" % (MIN_VIEWS, len(views)))
    keep = list(range(len(views)))
    prof, errs = _solve(views, objp, size, model)
    med = float(np.median(errs))
    bad = [keep[j] for j, e in enumerate(errs) if e > max(3.0 * med, 1.0)]
    if bad and len(keep) - len(bad) >= MIN_VIEWS:
        keep = [i for i in keep if i not in bad]
        prof, errs = _solve([views[i] for i in keep], objp, size, model)
    prof["views"] = len(keep)
    prof["views_dropped"] = len(views) - len(keep)
    return prof


def calibrate_best(img_points, board, size):
    """Fits both models and keeps the one that reprojects better. A lens
    past ~120 degrees is almost always the fisheye model: Brown-Conrady's
    polynomial cannot follow it into the corners."""
    results = {}
    for model in ("fisheye", "pinhole"):
        try:
            results[model] = calibrate(img_points, board, size, model)
        except (cv2.error, np.linalg.LinAlgError) as e:
            results[model] = e
    ok = {m: r for m, r in results.items() if isinstance(r, dict)}
    if not ok:
        raise ValueError("calibration failed: %s" % "; ".join("%s: %s" % kv for kv in results.items()))
    best = min(ok.values(), key=lambda r: r["rms_px"])
    best["alternatives"] = {m: (r["rms_px"] if isinstance(r, dict) else "failed") for m, r in results.items()}
    return best
