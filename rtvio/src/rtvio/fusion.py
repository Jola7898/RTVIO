"""
Geometry for turning per-window VGGT predictions into one consistent cloud,
with no GPS and no IMU - vision only.

WHY WINDOWS ARE ALIGNED WITH A DENSE SIM(3), NOT ONE SHARED CAMERA
VGGT reconstructs each window in its own frame AND its own scale (the
first camera of the window is the origin; the scale is whatever the model
picked for that window). The previous chaining (so3.rigid_from_pose_pair)
used one overlapping camera's 6-DOF pose - rigid only, so the per-window
scale difference went uncorrected and accumulated window after window.

Consecutive windows here share `overlap` whole frames, and VGGT predicts a
depth for every pixel of those frames in BOTH windows. The same pixel of
the same frame is the same physical point, so that is hundreds of thousands
of exact correspondences per window pair, with no feature matching at all.
robust_sim3 fits scale + rotation + translation to them (weighted Umeyama
inside iteratively-reweighted least squares, Cauchy weights on depth-
relative residuals, so near and far points count equally and the occasional
wrong-depth region is down-weighted instead of dragging the fit). This is
the same recipe as VGGT-Long (chunked VGGT + overlapping-chunk Sim(3)
alignment), minus its loop closure.

WHY VOXEL FUSION
Every frame at 30 fps sees nearly the same surface as its neighbours.
Concatenating all their pixels would give tens of millions of near-
duplicate points; averaging them per voxel (voxel ~ one pixel's footprint
at the scene's median depth) keeps the model's full resolution while
turning the redundancy into noise reduction. Each voxel also counts how
many distinct frames observed it - a point seen by one frame only is the
typical signature of a depth-edge artifact, which min_views removes.
"""
import threading

import numpy as np
import torch
import torch.nn.functional as F


# ------------------------------------------------------------ per pixel --

def unproject(depth, K, R_cw, C):
    """depth (S,H,W), K (S,3,3), R_cw (S,3,3) camera->world rotation,
    C (S,3) camera centre -> world points (S,H,W,3). Integer pixel grid,
    the same convention as vggt.utils.geometry.depth_to_cam_coords_points."""
    S, H, W = depth.shape
    dev, dt = depth.device, depth.dtype
    v, u = torch.meshgrid(torch.arange(H, device=dev, dtype=dt),
                          torch.arange(W, device=dev, dtype=dt), indexing="ij")
    fx, fy = K[:, 0, 0].view(S, 1, 1), K[:, 1, 1].view(S, 1, 1)
    cx, cy = K[:, 0, 2].view(S, 1, 1), K[:, 1, 2].view(S, 1, 1)
    cam = torch.stack([(u - cx) / fx * depth, (v - cy) / fy * depth, depth], dim=-1)
    return torch.einsum("sij,shwj->shwi", R_cw, cam) + C.view(S, 1, 1, 3)


def continuity_mask(depth, rel_thresh):
    """False on depth discontinuities (and one pixel either side of them).
    A pixel straddling a foreground/background edge gets a depth between the
    two surfaces - the "flying pixel" streaks that make an unfiltered depth
    cloud look smeared. A real surface, even at a grazing 80 degrees, changes
    depth by only ~tan(80)/fx ~ 1.4% per pixel at this resolution, so a
    per-pixel jump above rel_thresh (default 4%) is an edge, not geometry."""
    d = depth.unsqueeze(1)                                        # (S,1,H,W)
    inv = 1.0 / d.clamp_min(1e-6)
    gx = (d[..., :, 1:] - d[..., :, :-1]).abs() * torch.minimum(inv[..., :, 1:], inv[..., :, :-1])
    gy = (d[..., 1:, :] - d[..., :-1, :]).abs() * torch.minimum(inv[..., 1:, :], inv[..., :-1, :])
    g = torch.zeros_like(d)
    g[..., :, 1:] = torch.maximum(g[..., :, 1:], gx)
    g[..., :, :-1] = torch.maximum(g[..., :, :-1], gx)
    g[..., 1:, :] = torch.maximum(g[..., 1:, :], gy)
    g[..., :-1, :] = torch.maximum(g[..., :-1, :], gy)
    edge = F.max_pool2d((g > rel_thresh).float(), 3, stride=1, padding=1) > 0
    return ~edge.squeeze(1)


def pixel_normals(P, C):
    """Per-pixel normals of the world point map P (S,H,W,3) from central
    differences, oriented toward the observing camera C (S,3). Returns
    (normals, valid). Oriented normals are what makes Poisson reliable: with
    unoriented ones (e.g. a k-NN PCA fit) half the surface can come out
    inside-out and Poisson closes the wrong side."""
    S, H, W, _ = P.shape
    n = torch.zeros_like(P)
    dx = P[:, 1:-1, 2:] - P[:, 1:-1, :-2]
    dy = P[:, 2:, 1:-1] - P[:, :-2, 1:-1]
    n[:, 1:-1, 1:-1] = torch.cross(dx, dy, dim=-1)
    norm = n.norm(dim=-1, keepdim=True)
    valid = norm.squeeze(-1) > 0
    n = n / norm.clamp_min(1e-12)
    to_cam = C.view(S, 1, 1, 3) - P
    flip = (n * to_cam).sum(-1, keepdim=True) < 0
    n = torch.where(flip, -n, n)
    return n, valid


def percentile_threshold(values, pct, max_samples=4_000_000):
    """torch.quantile refuses inputs over 16M elements, and a 128-frame
    window of 518x294 confidences is 19.5M - so estimate the percentile
    from a random subsample, which is indistinguishable at this size."""
    v = values.reshape(-1)
    if v.numel() > max_samples:
        idx = torch.randint(0, v.numel(), (max_samples,), device=v.device)
        v = v[idx]
    return torch.quantile(v.float(), pct / 100.0).item()


def confidence_keep(conf, pct):
    """Torch twin of vggt_reconstruct.confidence_gate: the percentile is taken
    over pixels ABOVE the window's floor value (see that function's
    docstring for the real failure this avoids)."""
    floor = conf.min()
    above = conf[conf > floor]
    thresh = percentile_threshold(above, pct) if above.numel() else floor.item()
    return conf >= max(thresh, 1e-6), thresh


# ---------------------------------------------------------------- sim3 --

def weighted_umeyama(x, y, w):
    """Similarity (s, R, t) minimising sum w_i |s R x_i + t - y_i|^2.
    x, y: (N,3), w: (N,) - all float64 torch. Umeyama (1991) with weights."""
    ws = w.sum()
    mx = (w[:, None] * x).sum(0) / ws
    my = (w[:, None] * y).sum(0) / ws
    xc, yc = x - mx, y - my
    cov = (w[:, None, None] * (yc[:, :, None] * xc[:, None, :])).sum(0) / ws
    U, D, Vt = torch.linalg.svd(cov)
    Sd = torch.ones(3, dtype=x.dtype, device=x.device)
    if torch.linalg.det(U) * torch.linalg.det(Vt) < 0:
        Sd[2] = -1.0
    R = U @ torch.diag(Sd) @ Vt
    var_x = (w * (xc * xc).sum(1)).sum() / ws
    s = (D * Sd).sum() / var_x.clamp_min(1e-18)
    t = my - s * (R @ mx)
    return s, R, t


def _ransac_sim3(x, y, ref, hypotheses=512, n_eval=20_000, rel_thresh=0.05):
    """Best of `hypotheses` minimal 3-point Sim(3) fits, all solved at once as
    one batched SVD, scored by how many of n_eval points they put within
    rel_thresh (relative) of their target. Needed as IRLS's starting point:
    when a region's depth is wrong in one window, those outliers are all
    biased the same way, and IRLS started from the plain least-squares
    answer settles on a compromise scale (a unit test reproduces exactly
    that: 0.41 instead of 0.70 with 25% one-sided outliers)."""
    n = x.shape[0]
    g = torch.Generator(device="cpu").manual_seed(0)
    idx = torch.randint(0, n, (hypotheses, 3), generator=g).to(x.device)
    X, Y = x[idx], y[idx]                                            # (H,3,3)
    mx, my = X.mean(1, keepdim=True), Y.mean(1, keepdim=True)
    Xc, Yc = X - mx, Y - my
    cov = Yc.transpose(1, 2) @ Xc / 3.0
    U, D, Vt = torch.linalg.svd(cov)
    Sd = torch.ones_like(D)
    Sd[:, 2] = torch.sign(torch.linalg.det(U) * torch.linalg.det(Vt))
    R = U @ torch.diag_embed(Sd) @ Vt
    var_x = (Xc ** 2).sum((1, 2)) / 3.0
    s = (D * Sd).sum(1) / var_x.clamp_min(1e-18)
    t = my.squeeze(1) - s[:, None] * (R @ mx.transpose(1, 2)).squeeze(-1)
    e = torch.randint(0, n, (min(n_eval, n),), generator=g).to(x.device)
    pred = s[:, None, None] * (x[e][None] @ R.transpose(1, 2)) + t[:, None, :]
    r = (pred - y[e][None]).norm(dim=-1) / ref[e][None]
    score = (r < rel_thresh).sum(1)
    score[~torch.isfinite(s) | (s <= 0)] = -1
    b = int(score.argmax())
    return s[b], R[b], t[b]


def robust_sim3(x, y, scale_ref=None, iters=10, max_points=250_000, c=2.0):
    """Robust Sim(3) mapping x -> y from dense correspondences.

    scale_ref: (N,) positive per-point length scale (the depth of y in its
    own camera). Residuals are divided by it, so a 2 cm error on a wall 1 m
    away and a 20 cm error 10 m away weigh the same - otherwise the far
    background, whose absolute depth error is largest, dominates the fit.
    Starts from a batched 3-point RANSAC hypothesis (_ransac_sim3), then
    Cauchy-weighted IRLS: w = 1 / (1 + (r / (c * sigma))^2), sigma
    re-estimated each iteration from the median absolute residual.

    Returns (s, R, t, info) with info = dict(n, inlier_frac, median_rel_residual).
    """
    x = x.double()
    y = y.double()
    n = x.shape[0]
    if n > max_points:
        idx = torch.randperm(n, device=x.device)[:max_points]
        x, y = x[idx], y[idx]
        scale_ref = scale_ref[idx] if scale_ref is not None else None
    ref = scale_ref.double().clamp_min(1e-9) if scale_ref is not None else torch.ones_like(x[:, 0])
    s, R, t = _ransac_sim3(x, y, ref)
    for _ in range(iters):
        r = ((s * (x @ R.T) + t - y).norm(dim=1)) / ref
        sigma = 1.4826 * r.median().clamp_min(1e-12)
        w = 1.0 / (1.0 + (r / (c * sigma)) ** 2)
        s, R, t = weighted_umeyama(x, y, w)
    r = ((s * (x @ R.T) + t - y).norm(dim=1)) / ref
    sigma = 1.4826 * r.median().clamp_min(1e-12)
    info = {"n": int(x.shape[0]),
            "inlier_frac": float((r < 3 * sigma).float().mean()),
            "median_rel_residual": float(r.median())}
    return s, R, t, info


# ------------------------------------------------------------- fusion --

_OFF = 1 << 20          # voxel index offset: +-1M voxels per axis
_BITS = 21


class VoxelAccumulator:
    """Streaming voxel fusion. add() reduces one window's points to unique
    voxels on the GPU and hands the (small) result to a background thread
    that merges it into the running totals with numpy, so the GPU never
    waits on the merge."""

    MERGE_EVERY = 6

    def __init__(self, voxel_size):
        self.voxel = float(voxel_size)
        self._pending = []
        self._acc = None
        self._lock = threading.Lock()
        self._merge_thread = None
        self.n_points_in = 0

    def add(self, pts, cols, nrm, frame_ids):
        """pts (N,3) float, cols (N,3) uint8, nrm (N,3) float, frame_ids (N,)
        int64 - torch tensors on any device."""
        if pts.shape[0] == 0:
            return
        self.n_points_in += int(pts.shape[0])
        ijk = torch.floor(pts / self.voxel).long()
        ok = (ijk.abs() < _OFF).all(dim=1)
        if not bool(ok.all()):
            ijk, pts, cols, nrm, frame_ids = ijk[ok], pts[ok], cols[ok], nrm[ok], frame_ids[ok]
        key = ((ijk[:, 0] + _OFF) << (2 * _BITS)) | ((ijk[:, 1] + _OFF) << _BITS) | (ijk[:, 2] + _OFF)
        uniq, inv = torch.unique(key, return_inverse=True)
        m = uniq.shape[0]
        dev = pts.device
        sp = torch.zeros((m, 3), dtype=torch.float64, device=dev).index_add_(0, inv, pts.double())
        sc = torch.zeros((m, 3), dtype=torch.float32, device=dev).index_add_(0, inv, cols.float())
        sn = torch.zeros((m, 3), dtype=torch.float32, device=dev).index_add_(0, inv, nrm.float())
        cnt = torch.bincount(inv, minlength=m)
        span = int(frame_ids.max().item()) + 1
        pair = torch.unique(inv * span + frame_ids)
        views = torch.bincount(pair // span, minlength=m)
        part = {"k": uniq.cpu().numpy(), "p": sp.cpu().numpy(), "c": sc.cpu().numpy(),
                "n": sn.cpu().numpy(), "cnt": cnt.cpu().numpy().astype(np.int64),
                "v": views.cpu().numpy().astype(np.int64)}
        with self._lock:
            self._pending.append(part)
            ready = len(self._pending) >= self.MERGE_EVERY
        if ready and (self._merge_thread is None or not self._merge_thread.is_alive()):
            self._merge_thread = threading.Thread(target=self._merge, daemon=True)
            self._merge_thread.start()

    def _merge(self):
        with self._lock:
            parts, self._pending = self._pending, []
            acc = self._acc
        if acc is not None:
            parts = [acc] + parts
        if not parts:
            return
        k = np.concatenate([p["k"] for p in parts])
        uniq, inv = np.unique(k, return_inverse=True)
        m = len(uniq)
        merged = {"k": uniq}
        for name, width in (("p", 3), ("c", 3), ("n", 3)):
            src = np.concatenate([p[name] for p in parts])
            merged[name] = np.stack([np.bincount(inv, weights=src[:, j], minlength=m)
                                     for j in range(width)], axis=1)
        for name in ("cnt", "v"):
            src = np.concatenate([p[name] for p in parts])
            merged[name] = np.bincount(inv, weights=src, minlength=m).astype(np.int64)
        with self._lock:
            self._acc = merged

    def finish(self, min_views=1):
        """Averaged cloud: (points float64, colors uint8, normals float32, views)."""
        if self._merge_thread is not None:
            self._merge_thread.join()
        self._merge()
        acc = self._acc
        if acc is None:
            z = np.zeros((0, 3))
            return z, z.astype(np.uint8), z.astype(np.float32), np.zeros(0, np.int64)
        keep = acc["v"] >= min_views
        cnt = acc["cnt"][keep][:, None].astype(np.float64)
        pts = acc["p"][keep] / cnt
        cols = np.clip(acc["c"][keep] / cnt + 0.5, 0, 255).astype(np.uint8)
        nrm = acc["n"][keep]
        nrm = (nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)).astype(np.float32)
        return pts, cols, nrm, acc["v"][keep]

    @property
    def n_voxels(self):
        acc = self._acc
        return 0 if acc is None else len(acc["k"])
