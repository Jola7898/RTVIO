"""
CPU tests for the vision-only fusion math (rtvio/fusion.py) and the binary
PLY writers (rtvio/surface.py). No GPU or checkpoint needed.

    python tests/test_fusion.py
"""
import os
import tempfile

import numpy as np
import torch

from rtvio.fusion import (robust_sim3, weighted_umeyama, unproject, pixel_normals,
                          continuity_mask, confidence_keep, VoxelAccumulator)
from rtvio.surface import write_ply_points, write_ply_mesh, poisson_mesh

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-64s %s" % ("PASS" if ok else "FAIL", name, detail))


def _rot(axis, deg):
    a = np.radians(deg)
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(a) * K + (1 - np.cos(a)) * K @ K


def test_umeyama_exact():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(500, 3))
    R = _rot([1, 2, 3], 37.0)
    s, t = 2.5, np.array([1.0, -2.0, 0.5])
    y = s * x @ R.T + t
    s_, R_, t_ = weighted_umeyama(torch.tensor(x), torch.tensor(y), torch.ones(500, dtype=torch.float64))
    check("weighted Umeyama recovers an exact Sim(3)",
          abs(float(s_) - s) < 1e-9 and np.allclose(R_.numpy(), R, atol=1e-9)
          and np.allclose(t_.numpy(), t, atol=1e-9), "s=%.6f" % float(s_))


def test_robust_sim3_with_outliers_and_depth_noise():
    # The window-to-window case: y = the previous window's (global) points,
    # x = the same pixels in the new window's frame, which differs by a
    # scale change as well as a rigid motion. 25% of pixels get a grossly
    # wrong depth in one window (the kind of region VGGT gets wrong), and
    # every point carries 1% depth noise.
    rng = np.random.default_rng(1)
    n = 40_000
    depth = rng.uniform(0.5, 8.0, size=n)
    dirs = rng.normal(size=(n, 3))
    dirs[:, 2] = np.abs(dirs[:, 2]) + 1.0
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    y = dirs * depth[:, None]
    R = _rot([0.3, 1, 0.2], 12.0)
    s, t = 0.7, np.array([0.3, 0.1, -0.4])
    x = ((y - t) @ R) / s                                          # exact inverse map
    x *= (1 + 0.01 * rng.normal(size=n))[:, None]
    bad = rng.random(n) < 0.25
    x[bad] *= rng.uniform(1.3, 3.0, size=(bad.sum(), 1))
    s_, R_, t_, info = robust_sim3(torch.tensor(x), torch.tensor(y), torch.tensor(depth))
    ang = np.degrees(np.arccos(np.clip((np.trace(R_.numpy().T @ R) - 1) / 2, -1, 1)))
    check("robust Sim(3): scale within 1% despite 25% outliers",
          abs(float(s_) / s - 1) < 0.01, "s=%.4f (true %.4f)" % (float(s_), s))
    check("robust Sim(3): rotation within 0.3 deg", ang < 0.3, "%.3f deg" % ang)
    s_p, R_p, t_p = weighted_umeyama(torch.tensor(x), torch.tensor(y), torch.ones(n, dtype=torch.float64))
    check("plain least squares is NOT robust to the same outliers (sanity)",
          abs(float(s_p) / s - 1) > 0.05, "plain s=%.4f" % float(s_p))
    check("robust Sim(3) reports inlier fraction near 75%",
          0.6 < info["inlier_frac"] < 0.9, "%.2f" % info["inlier_frac"])


def test_unproject_and_normals():
    # A fronto-parallel wall at depth 2 seen by an identity camera: every
    # point has z=2, and every normal must face back toward the camera (-z).
    S, H, W = 1, 24, 32
    depth = torch.full((S, H, W), 2.0)
    K = torch.tensor([[[40.0, 0, 16], [0, 40.0, 12], [0, 0, 1]]])
    R = torch.eye(3)[None]
    C = torch.zeros(1, 3)
    P = unproject(depth, K, R, C)
    check("unproject puts a constant-depth map on the z=2 plane", torch.allclose(P[..., 2], depth))
    check("unproject: principal point maps to the optical axis",
          torch.allclose(P[0, 12, 16], torch.tensor([0.0, 0.0, 2.0])))
    n, valid = pixel_normals(P, C)
    inner = n[0, 1:-1, 1:-1]
    check("normals of a wall face the camera", torch.allclose(inner, torch.tensor([0.0, 0.0, -1.0]).expand_as(inner), atol=1e-6))
    check("border pixels have no normal", not bool(valid[0, 0].any()))


def test_continuity_mask():
    depth = torch.full((1, 20, 20), 2.0)
    depth[:, :, 10:] = 5.0                       # a depth edge at column 10
    ok = continuity_mask(depth, 0.04)
    check("continuity mask removes both sides of a depth edge", not bool(ok[0, 5, 9]) and not bool(ok[0, 5, 10]))
    check("continuity mask keeps flat regions", bool(ok[0, 5, 2]) and bool(ok[0, 5, 17]))
    ramp = torch.linspace(1.0, 1.2, 20).view(1, 1, 20).expand(1, 20, 20).clone()
    check("continuity mask keeps a smoothly slanted surface", bool(continuity_mask(ramp, 0.04).all()))


def test_confidence_keep_matches_numpy_gate():
    from rtvio.vggt_reconstruct import confidence_gate
    rng = np.random.default_rng(0)
    conf = np.concatenate([np.ones(8500), rng.uniform(1.5, 8.0, size=1500)]).astype(np.float32)
    keep_np, t_np = confidence_gate(conf, 50)
    keep_t, t_t = confidence_keep(torch.tensor(conf), 50)
    check("torch confidence gate agrees with the numpy one", abs(t_np - t_t) < 1e-3
          and abs(keep_np.mean() - keep_t.float().mean().item()) < 1e-3, "%.4f vs %.4f" % (t_np, t_t))


def test_voxel_accumulator():
    acc = VoxelAccumulator(voxel_size=1.0)
    acc.MERGE_EVERY = 2
    # Two windows observing the same two voxels; one extra voxel is seen by a
    # single frame only and must be dropped by min_views=2.
    p1 = torch.tensor([[0.2, 0.2, 0.2], [0.4, 0.4, 0.4], [5.5, 0.5, 0.5]])
    c1 = torch.tensor([[100, 0, 0], [200, 0, 0], [0, 0, 255]], dtype=torch.uint8)
    n1 = torch.tensor([[0, 0, 1.0]] * 3)
    acc.add(p1, c1, n1, torch.tensor([0, 1, 0]))
    p2 = torch.tensor([[0.6, 0.6, 0.6], [5.5, 0.5, 0.5]])
    acc.add(p2, c1[:2], n1[:2], torch.tensor([7, 7]))
    acc.add(torch.tensor([[-3.5, 0.1, 0.1]]), c1[:1], n1[:1], torch.tensor([9]))
    pts, cols, nrm, views = acc.finish(min_views=2)
    order = np.argsort(pts[:, 0])
    pts, cols, views = pts[order], cols[order], views[order]
    check("voxel fusion keeps voxels seen by >= 2 frames", len(pts) == 2, "%d points" % len(pts))
    check("fused position is the mean of its samples", np.allclose(pts[0], [0.4, 0.4, 0.4]), pts[0])
    # voxel (0,0,0) got red 100 and 200 from window 1 and 100 from window 2
    check("fused colour is the mean of its samples", cols[0, 0] == 133, cols[0])
    check("views counts distinct frames, not samples", list(views) == [3, 2], list(views))


def test_ply_writers_roundtrip():
    tmp = tempfile.mkdtemp()
    pts = np.random.default_rng(0).normal(size=(100, 3)).astype(np.float32)
    cols = (np.arange(300) % 256).reshape(100, 3).astype(np.uint8)
    path = os.path.join(tmp, "c.ply")
    write_ply_points(path, pts, cols, normals=pts, scalars={"views": np.ones(100, np.float32)})
    raw = open(path, "rb").read()
    head, body = raw.split(b"end_header\n", 1)
    check("point PLY header ends with a bare LF (strict readers need it)", b"\r" not in head)
    check("point PLY body size matches 100 x (3+3 floats + 3 bytes + 1 float)", len(body) == 100 * (24 + 3 + 4), len(body))
    import trimesh
    loaded = trimesh.load(path)
    check("trimesh reads the point PLY back", np.allclose(np.asarray(loaded.vertices), pts, atol=1e-6))
    verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)
    mp = os.path.join(tmp, "m.ply")
    write_ply_mesh(mp, verts, np.array([[0, 1, 2]], np.int32), cols=cols[:3], normals=verts)
    m = trimesh.load(mp)
    check("trimesh reads the mesh PLY back", len(m.faces) == 1 and np.allclose(m.vertices, verts))


def test_poisson_trims_invented_surface():
    # Oriented samples of the upper half of a unit sphere only. Poisson
    # closes the bottom; the distance trim must remove that invented half.
    rng = np.random.default_rng(0)
    d = rng.normal(size=(60000, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    d = d[d[:, 2] > 0.1]
    cols = np.full((len(d), 3), 128, np.uint8)
    verts, faces, vc, vn, info = poisson_mesh(d, d, cols, depth=7, trim_dist=0.05, log=lambda *a: None)
    check("poisson produced a mesh", len(faces) > 1000, info)
    check("distance trim removed the invented lower half", verts[:, 2].min() > 0.0, "min z %.3f" % verts[:, 2].min())


if __name__ == "__main__":
    import sys
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
