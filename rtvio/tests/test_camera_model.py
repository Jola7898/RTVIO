"""
Checks for lens calibration and undistortion: camera_model.py, the Studio's
checkerboard capture (studio/camera_calib.py + drone_link.py) and the
undistorting FrameLoader in vggt_reconstruct.

    python tests/test_camera_model.py

CPU-only - no drone, no GPU, no printed board. Checkerboard images are
rendered through a known fisheye lens (~140 deg across, about what the
iDronam drone's camera covers), so every number the calibration recovers
is checked against the truth, and "straight lines come out straight" is
measured rather than eyeballed.

Imports `rtvio` as an installed package (`pip install -e .` from the repo
root) rather than patching sys.path - see pyproject.toml.
"""
import json
import math
import os
import shutil
import tempfile
import threading
import time

import cv2
import numpy as np

from rtvio import camera_model
from rtvio.studio.camera_calib import CalibrationSession
from rtvio.studio.drone_link import DroneLink
from rtvio.vggt_reconstruct import FrameLoader, _lens_profile

W, H = 1280, 720
TRUE = camera_model.normalize({"model": "fisheye", "width": W, "height": H, "fx": 520.0, "fy": 518.0,
                               "cx": 641.3, "cy": 357.8, "k1": 0.05, "k2": -0.02, "k3": 0.006, "k4": -0.001})
K_TRUE, D_TRUE = camera_model.matrices(TRUE)
BOARD = (9, 6)                  # inner corners
PPS, MARGIN = 48, 1             # texture pixels per square, white border in squares

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-62s %s" % ("PASS" if ok else "FAIL", name, detail))


def wait_until(cond, timeout):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# -------------------------------------------------------------- rendering --

def board_texture():
    cols, rows = BOARD
    tex = np.full(((rows + 1 + 2 * MARGIN) * PPS, (cols + 1 + 2 * MARGIN) * PPS), 255, np.uint8)
    for j in range(rows + 1):
        for i in range(cols + 1):
            if (i + j) % 2 == 0:
                y0, x0 = (j + MARGIN) * PPS, (i + MARGIN) * PPS
                tex[y0:y0 + PPS, x0:x0 + PPS] = 0
    return tex


TEX = board_texture()
_RAYS = None


def rays():
    """The ray every pixel of the true lens sees, (W*H, 3)."""
    global _RAYS
    if _RAYS is None:
        uu, vv = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
        xy = camera_model.normalized_rays(TRUE, np.stack([uu.ravel(), vv.ravel()], 1))
        _RAYS = np.concatenate([xy, np.ones((len(xy), 1))], 1)
    return _RAYS


def render(R, t):
    """The board at pose (R, t) seen through the true fisheye lens: each
    pixel's ray is intersected with the board plane and the texture sampled
    there - the lens model applied exactly, not approximated."""
    d = rays()
    n = R[:, 2]
    denom = d @ n
    s = float(t @ n) / np.where(np.abs(denom) < 1e-9, 1e-9, denom)
    B = (d * s[:, None] - t) @ R                        # board coordinates, rows = R^T (P - t)
    mapx = ((MARGIN + 1 + B[:, 0]) * PPS - 0.5).reshape(H, W).astype(np.float32)
    mapy = ((MARGIN + 1 + B[:, 1]) * PPS - 0.5).reshape(H, W).astype(np.float32)
    mapx[(s <= 0).reshape(H, W)] = -1e4
    img = cv2.remap(TEX, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=150)
    return cv2.cvtColor(cv2.GaussianBlur(img, (0, 0), 0.7), cv2.COLOR_GRAY2BGR)


def poses(n, seed=1):
    """n board poses spread over the view (corners included), fully in frame."""
    rng = np.random.default_rng(seed)
    objp = camera_model.board_points(*BOARD)
    centre = objp.mean(axis=0)
    out, tries = [], 0
    while len(out) < n and tries < 20000:
        tries += 1
        u, v = rng.uniform(0.1, 0.9) * W, rng.uniform(0.12, 0.88) * H
        ray = np.append(camera_model.normalized_rays(TRUE, [[u, v]])[0], 1.0)
        R = cv2.Rodrigues(rng.uniform(-1, 1, 3) * [0.6, 0.6, 0.3])[0]
        t = ray * rng.uniform(6.0, 13.0) - R @ centre
        proj = cv2.fisheye.projectPoints(objp.reshape(-1, 1, 3), cv2.Rodrigues(R)[0], t.reshape(3, 1),
                                         K_TRUE, D_TRUE)[0].reshape(-1, 2)
        if proj[:, 0].min() < 20 or proj[:, 0].max() > W - 20 or proj[:, 1].min() < 20 or proj[:, 1].max() > H - 20:
            continue
        g = proj.reshape(BOARD[1], BOARD[0], 2)
        if min(np.linalg.norm(np.diff(g, axis=1), axis=2).min(), np.linalg.norm(np.diff(g, axis=0), axis=2).min()) < 14:
            continue
        out.append((R, t))
    return out


def line_residual(pts):
    """Largest distance (px) of 2D points from their best-fit straight line."""
    p = np.asarray(pts, np.float64).reshape(-1, 2)
    c = p - p.mean(axis=0)
    normal = np.linalg.svd(c)[2][1]
    return float(np.abs(c @ normal).max())


def straight_line_through_lens():
    """A straight 3D edge spanning ~110 deg of the view, as the true lens images it."""
    X = np.stack([np.linspace(-4, 4, 41), np.full(41, 1.2), np.full(41, 3.0)], 1)
    return cv2.fisheye.projectPoints(X.reshape(-1, 1, 3), np.zeros((3, 1)), np.zeros((3, 1)), K_TRUE, D_TRUE)[0]


# ------------------------------------------------------------------ tests --

def test_profile_math():
    q = camera_model.scaled(TRUE, 640, 360)
    check("scaled: halves the focal length and principal point",
          abs(q["fx"] - 260.0) < 1e-9 and abs(q["fy"] - 259.0) < 1e-9 and abs(q["cx"] - 320.4) < 1e-9,
          "fx %.2f cx %.2f" % (q["fx"], q["cx"]))
    check("scaled: refuses a different aspect ratio (a crop)", camera_model.scaled(TRUE, 640, 480) is None)
    pin = camera_model.normalize({"fx": 500, "fy": 500, "cx": 499.5, "cy": 299.5, "width": 1000, "height": 600})
    hf = camera_model.fov_deg(pin)[0]
    check("fov: a distortion-free pinhole matches 2*atan(w/2f)", abs(hf - 90.0) < 1e-6 and not camera_model.has_distortion(pin),
          "%.4f deg" % hf)

    # The fisheye FOV worked out independently of OpenCV: solve
    # theta_d(theta) = theta (1 + k1 th^2 + k2 th^4 + k3 th^6 + k4 th^8) = r at each edge.
    k = [TRUE[x] for x in ("k1", "k2", "k3", "k4")]

    def theta(r):
        lo, hi = 0.0, math.pi / 2
        for _ in range(100):
            mid = (lo + hi) / 2
            td = mid * (1 + k[0] * mid ** 2 + k[1] * mid ** 4 + k[2] * mid ** 6 + k[3] * mid ** 8)
            lo, hi = (mid, hi) if td < r else (lo, mid)
        return lo
    truth = math.degrees(theta((TRUE["cx"] + 0.5) / TRUE["fx"]) + theta((W - 0.5 - TRUE["cx"]) / TRUE["fx"]))
    got = camera_model.fov_deg(TRUE)[0]
    check("fov: fisheye edge-to-edge angle matches the lens equation", abs(got - truth) < 0.05,
          "%.2f vs %.2f deg" % (got, truth))


def test_undistort_straightens():
    m1, m2, K_new = camera_model.undistort_maps(TRUE)
    pts = straight_line_through_lens()
    raw = line_residual(pts)
    und = line_residual(cv2.fisheye.undistortPoints(pts, K_TRUE, D_TRUE, P=K_new))
    check("undistort: a straight edge the lens bends comes out straight",
          raw > 15 and und < 0.2, "bow %.1f px through the lens -> %.3f px undistorted" % (raw, und))
    mapx, mapy = cv2.convertMaps(m1, m2, cv2.CV_32FC1)
    uu, vv = np.meshgrid(np.linspace(0, W - 1, 9).astype(int), np.linspace(0, H - 1, 7).astype(int))
    dest = np.stack([uu.ravel(), vv.ravel(), np.ones(uu.size)], 1).astype(np.float64)
    ray = (np.linalg.inv(K_new) @ dest.T).T
    src = cv2.fisheye.projectPoints(ray.reshape(-1, 1, 3), np.zeros((3, 1)), np.zeros((3, 1)), K_TRUE, D_TRUE)[0].reshape(-1, 2)
    err = np.hypot(mapx[vv.ravel(), uu.ravel()] - src[:, 0], mapy[vv.ravel(), uu.ravel()] - src[:, 1]).max()
    check("undistort: remap tables follow the lens model", err < 0.06, "max %.3f px" % err)
    outside = np.mean((mapx < -0.5) | (mapx > W - 0.5) | (mapy < -0.5) | (mapy > H - 0.5))
    check("undistort: balance 0 leaves no black border", outside < 0.005, "%.3f%% of pixels outside" % (100 * outside))


def test_calibration_recovers_lens(tmp):
    sess = CalibrationSession(BOARD[0], BOARD[1], os.path.join(tmp, "views"))
    sess.stop()                                 # driven synchronously below instead of by its worker
    pv = poses(24)
    added = sum(sess.detect(render(R, t)) for R, t in pv)
    check("capture: board found and kept in most rendered views", added >= 18, "%d of %d" % (added, len(pv)))
    check("capture: views saved as JPEGs", len([f for f in os.listdir(sess.save_dir) if f.endswith(".jpg")]) == added)
    check("capture: coverage reaches all 9 image regions", int((np.array(sess.coverage) > 0).sum()) == 9,
          str(sess.coverage.tolist()))
    prof = sess.solve()
    alt = prof.get("alternatives", {})
    check("solve: picks the fisheye model over the pinhole one",
          prof["model"] == "fisheye" and isinstance(alt.get("pinhole"), float) and alt["pinhole"] > prof["rms_px"],
          str(alt))
    check("solve: focal length within 1%",
          abs(prof["fx"] / 520.0 - 1) < 0.01 and abs(prof["fy"] / 518.0 - 1) < 0.01,
          "fx %.1f (520) fy %.1f (518)" % (prof["fx"], prof["fy"]))
    check("solve: principal point within 3 px",
          abs(prof["cx"] - 641.3) < 3 and abs(prof["cy"] - 357.8) < 3, "%.1f, %.1f" % (prof["cx"], prof["cy"]))
    check("solve: sub-pixel reprojection error", prof["rms_px"] < 0.5, "%.3f px" % prof["rms_px"])
    fov_got, fov_true = camera_model.fov_deg(prof)[0], camera_model.fov_deg(TRUE)[0]
    check("solve: field of view within 1 deg", abs(fov_got - fov_true) < 1.0, "%.1f vs %.1f deg" % (fov_got, fov_true))
    _m1, _m2, K_new = camera_model.undistort_maps(prof)
    res = line_residual(camera_model.undistort_points(prof, straight_line_through_lens(), K_new))
    check("solve: the recovered lens straightens the true lens's bent edge", res < 1.0, "%.3f px" % res)
    return prof


def test_frameloader(tmp):
    frames = os.path.join(tmp, "frames")
    os.makedirs(frames)
    paths = []
    for i, (R, t) in enumerate(poses(3, seed=7)):
        paths.append(os.path.join(frames, "%06d.jpg" % i))
        cv2.imwrite(paths[-1], render(R, t))
    loader = FrameLoader(paths, camera=TRUE)
    u = loader.undistort_info or {}
    rgb, _ = loader._load(0)
    loader.close()
    check("FrameLoader: undistorts with a fisheye profile, then resizes for VGGT",
          u.get("model") == "fisheye" and rgb.shape == (294, 518, 3) and u["pinhole_hfov"] < u["lens_hfov"],
          "lens %.0f deg -> pinhole %.0f deg" % (u.get("lens_hfov", 0), u.get("pinhole_hfov", 0)))
    loader = FrameLoader(paths, camera=dict(TRUE, height=960))
    check("FrameLoader: skips a calibration of a different aspect", loader.undistort_info is None)
    loader.close()

    prof_path = os.path.join(tmp, "lens.json")
    with open(prof_path, "w") as f:
        json.dump(TRUE, f)
    phone_path = os.path.join(tmp, "phone.json")
    with open(phone_path, "w") as f:
        json.dump({"fx_pix": 900.0, "fy_pix": 900.0, "cx_pix": 640.0, "cy_pix": 360.0, "k1": 0.0, "k2": 0.0,
                   "p1": 0.0, "p2": 0.0, "k3": 0.0, "source": "Camera2"}, f)
    check("lens profile: a session's calibration is used", _lens_profile(prof_path) is not None)
    check("lens profile: --no-undistort turns it off", _lens_profile(prof_path, undistort=False) is None)
    check("lens profile: phone Camera2 intrinsics never trigger undistortion", _lens_profile(phone_path) is None)
    check("lens profile: --intrinsics fills in for a take without its own",
          _lens_profile(os.path.join(tmp, "missing.json"), fallback=prof_path) is not None)


def test_studio_end_to_end(tmp):
    """Drone tab -> Camera calibration, for real: DroneLink reads a video of
    a moving board (standing in for the drone's stream), captures views,
    solves, saves data/drone_camera.json - then a take records
    camera_intrinsics.json scaled to its recorded size."""
    clip = os.path.join(tmp, "board.avi")
    w = cv2.VideoWriter(clip, cv2.VideoWriter_fourcc(*"MJPG"), 12, (W, H))
    for R, t in poses(16, seed=3):
        img = render(R, t)
        for _ in range(6):
            w.write(img)
    w.release()

    done, finalized = [], threading.Event()

    def on_final(d, meta):
        done.append((d, meta))
        finalized.set()

    cfg = {"enabled": True, "ip": "", "mavlink_port": 14550, "video_url": clip, "mode": "indoor",
           "record_long_side": 640, "jpeg_quality": 90, "video_delay_ms": 0, "preview_undistort": False}
    cam_path = os.path.join(tmp, "drone_camera.json")
    link = DroneLink(os.path.join(tmp, "sessions"), cfg, on_session_finalized=on_final,
                     camera_path=cam_path, calib_root=os.path.join(tmp, "calib"))
    link.start()
    try:
        if not wait_until(lambda: link.snapshot()["video"]["connected"], 15):
            check("studio: video connected", False)
            return
        ok, err = link.start_calibration(*BOARD)
        got = wait_until(lambda: (link.snapshot()["calibration"] or {}).get("views", 0) >= 12, 90)
        cal = link.snapshot()["calibration"]
        check("studio: views captured from the live video", ok and got, "%s views, status: %s" % (cal["views"], cal["status"]))
        ok, s = link.solve_calibration()
        check("studio: calibrated and saved", ok and os.path.exists(cam_path) and link.snapshot()["camera"] is not None,
              str(s) if not ok else "fisheye fx %.1f, %.0f x %.0f deg, RMS %.2f px" % (s["fx"], s["hfov"], s["vfov"], s["rms_px"]))
        if not ok:
            return
        check("studio: calibration focal within 2% of the truth", s["model"] == "fisheye" and abs(s["fx"] / 520.0 - 1) < 0.02,
              "fx %.1f" % s["fx"])

        link.configure(dict(cfg, preview_undistort=True))
        check("studio: undistorted preview rendered",
              wait_until(lambda: link._preview_maps is not None, 5))

        ok, sid = link.start_recording()
        time.sleep(1.0)
        link.stop_recording()
        if not (ok and finalized.wait(15)):
            check("studio: take recorded", False, str(sid))
            return
        d, meta = done[0]
        ci = camera_model.load_profile(os.path.join(d, "camera_intrinsics.json"))
        check("studio: the take carries the calibration, scaled to its recorded size",
              ci is not None and ci["width"] == 640 and abs(ci["fx"] / (link.camera["fx"] / 2) - 1) < 1e-9
              and meta["camera"]["model"] == "fisheye",
              "%sx%s fx %.1f" % (ci and ci["width"], ci and ci["height"], ci["fx"] if ci else 0))
    finally:
        link.configure(dict(cfg, enabled=False))
        time.sleep(0.5)


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="rtvio_lens_")
    try:
        test_profile_math()
        test_undistort_straightens()
        test_calibration_recovers_lens(tmp)
        test_frameloader(tmp)
        test_studio_end_to_end(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
