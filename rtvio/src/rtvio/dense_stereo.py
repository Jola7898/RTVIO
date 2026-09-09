"""
Stage: turns the sparse ORB point cloud (tracking.py, a few thousand
points - not remotely "dense reconstruction covering terrain, structures,
roads, vegetation" as the PS requires) into a dense colored point cloud,
using only cv2 + numpy + scipy (no pycolmap/Open3D - neither has a
Windows wheel for this machine's Python 3.13, see the plan doc).

Method: pose-seeded multi-view plane-sweep stereo, not rectified block
matching. Repeated attempts with cv2.stereoRectify/StereoSGBM each turned
up a different convention mismatch for this near-nadir forward-flight
geometry, so rectification is gone entirely: for a sweep of candidate
depths, warp each source image into the reference frame using ONLY the
plain perspective-projection convention (K, R, p), score every pixel's
photometric agreement at every depth, and keep the best-scoring depth.
The only geometry involved is "project a 3D point to a pixel", exercised
identically to how tracking.py projects.

Four things distinguish this from the naive version of that sweep, and
each of them was worth several metres of accuracy:

1. Depth sampling is uniform in INVERSE depth, not in depth (and not
   geometric). Disparity is linear in inverse depth, so uniform-inverse-
   depth planes are uniform in the quantity the images can actually
   resolve; N_PLANES is chosen so consecutive planes differ by well under
   a pixel of disparity for the pair's actual baseline. The earlier
   version swept 48 geometrically-spaced planes over 5-250 m, which at
   the ~85 m scene depth put consecutive planes ~7 m apart - so the
   winner-take-all depth could not be better than several metres wrong
   no matter how good the matching was.

2. The depth RANGE is taken per keyframe from the sparse map points
   tracking.py already triangulated (projected into the reference view,
   robust percentiles, padded), instead of a fixed 5-250 m. Spending all
   the planes on the depths the scene actually occupies is what makes
   point 1 affordable.

3. The cost is windowed ZNCC, not SSD. The input is compressed video with
   a moving sun-lit scene; SSD punishes the brightness/contrast
   differences between two views of the same surface, ZNCC is invariant
   to them. ZNCC also gives a scale-free score, which is what lets the
   accept/uniqueness thresholds below be fixed constants.

4. Costs are aggregated over SEVERAL source frames per reference frame,
   and a match is only accepted if it is both good in absolute terms
   (MAX_ACCEPT_COST), unambiguous (UNIQUENESS_MARGIN better than the best
   competing depth elsewhere in the sweep) and in a textured part of the
   reference image (MIN_REF_VARIANCE). A single pair on near-repetitive
   aerial texture produces a large minority of confidently-wrong matches;
   these three tests are what remove them.

The winning plane is finally refined sub-plane by fitting a parabola to
the cost at the winner and its two neighbours, in inverse-depth space.
"""
import numpy as np
import cv2
from scipy.spatial import cKDTree

from .tracking import CV_FROM_BODY

MIN_STEREO_BASELINE_M = 4.0
MAX_STEREO_BASELINE_M = 20.0
MIN_DEPTH_M = 5.0           # fallback sweep range, used only when the sparse
MAX_DEPTH_M = 250.0         # map gives no usable depth prior for a keyframe
SEARCH_AHEAD_FRAMES = 90    # how far forward to look for a stereo partner
MAX_SOURCE_VIEWS = 3        # source frames aggregated per reference frame
KEYFRAME_STRIDE = 8         # only run stereo from every Nth frame (cost control)
COST_WINDOW = 7             # box-filter size for the ZNCC support window

TARGET_DISPARITY_STEP_PX = 0.5   # plane spacing, in disparity terms
MIN_PLANES = 24
MAX_PLANES = 96

MAX_ACCEPT_COST = 0.35      # 1 - ZNCC; 0.35 means ZNCC >= 0.65
UNIQUENESS_MARGIN = 0.05    # runner-up (outside the winner's basin) must be
                            # at least this much worse, or the match is called
                            # ambiguous and dropped
UNIQUENESS_EXCLUDE_PX = 3.0  # half-width of "the winner's own basin", in
                            # DISPARITY pixels rather than plane indices: with
                            # sub-pixel plane spacing the correct minimum spans
                            # many planes, so a fixed +/-2-plane exclusion
                            # compares the winner against itself and reports
                            # every good match as ambiguous.
MIN_REF_VARIANCE = 3.0      # per-window intensity variance in the reference
                            # image below which ZNCC is meaningless noise.
                            # Deliberately low: this synthetic terrain's own
                            # texture sits at ~2 grey levels of standard
                            # deviation, so anything stricter throws away the
                            # ground and keeps only rooftops and road edges.
MIN_VALID_FRACTION = 0.99   # fraction of the support window that must warp
                            # inside the source image


def find_stereo_partners(poses, i, max_views=MAX_SOURCE_VIEWS):
    """poses: list of (R, p) world poses, index-aligned with frames.
    Returns up to `max_views` later frames whose baseline from frame i
    falls in [MIN_STEREO_BASELINE_M, MAX_STEREO_BASELINE_M], spread over
    that range rather than bunched at its start (a wide-baseline view
    resolves depth finely, a narrow one is less likely to be occluded or
    ambiguous - aggregating both is the point)."""
    p_i = poses[i][1]
    candidates = []
    for j in range(i + 1, min(i + 1 + SEARCH_AHEAD_FRAMES, len(poses))):
        baseline = np.linalg.norm(poses[j][1] - p_i)
        if baseline > MAX_STEREO_BASELINE_M:
            break
        if baseline >= MIN_STEREO_BASELINE_M:
            candidates.append((j, baseline))
    if not candidates:
        return []
    if len(candidates) <= max_views:
        return [j for j, _ in candidates]
    targets = np.linspace(0, len(candidates) - 1, max_views)
    return [candidates[int(round(t))][0] for t in targets]


def find_stereo_partner(poses, i):
    """Single-partner convenience wrapper (kept for callers/tests that
    only want one)."""
    partners = find_stereo_partners(poses, i, max_views=1)
    return partners[0] if partners else None


def _world_to_pixels(Xw, R, p, K):
    """Xw: Nx3 world points. R, p: this repo's body-to-world camera
    convention (forward = -R[:,2], up = +R[:,1] - the same convention
    tracking.py's poses are expressed in). Returns (Nx2 pixel coords,
    N depths along the view direction)."""
    Xc_cv = (Xw - p) @ R @ CV_FROM_BODY.T   # row-vector form of S @ R.T @ (Xw-p)
    depth = Xc_cv[:, 2]
    safe_depth = np.where(np.abs(depth) < 1e-6, 1e-6, depth)
    u = K[0, 0] * (Xc_cv[:, 0] / safe_depth) + K[0, 2]
    v = K[1, 1] * (Xc_cv[:, 1] / safe_depth) + K[1, 2]
    return np.stack([u, v], axis=1), depth


def _pixel_to_world_at_depth(u, v, depth, R, p, K):
    """Inverse of _world_to_pixels for a whole image grid: given every
    pixel's (u,v) and an assumed depth (distance along the camera's view
    direction), returns the world point each pixel corresponds to."""
    x_cv = (u - K[0, 2]) / K[0, 0] * depth
    y_cv = (v - K[1, 2]) / K[1, 1] * depth
    Xc_cv = np.stack([x_cv, y_cv, depth], axis=-1)
    return Xc_cv @ CV_FROM_BODY @ R.T + p   # row-vector form of R @ S @ Xc_cv + p


def depth_range_from_sparse(sparse_points, R, p, K, img_shape,
                            pad_frac=0.25, min_points=25):
    """Robust [near, far] sweep bounds for one reference view, read off
    the sparse map tracking.py already built: project every map point
    into this view, keep the ones that land in the image in front of the
    camera, and take padded 5th/95th percentiles of their depth. Returns
    None when too few points land here to trust the estimate, in which
    case the caller falls back to MIN_DEPTH_M/MAX_DEPTH_M."""
    if sparse_points is None or len(sparse_points) < min_points:
        return None
    h, w = img_shape[:2]
    pix, depth = _world_to_pixels(np.asarray(sparse_points, dtype=np.float64), R, p, K)
    ok = (
        (depth > MIN_DEPTH_M) & (depth < MAX_DEPTH_M)
        & (pix[:, 0] >= 0) & (pix[:, 0] < w)
        & (pix[:, 1] >= 0) & (pix[:, 1] < h)
    )
    if ok.sum() < min_points:
        return None
    d = depth[ok]
    near, far = np.percentile(d, 5.0), np.percentile(d, 95.0)
    span = max(far - near, 5.0)
    near = max(MIN_DEPTH_M, near - pad_frac * span)
    far = min(MAX_DEPTH_M, far + pad_frac * span)
    if far <= near * 1.02:
        return None
    return float(near), float(far)


def depth_range_from_coarse_sweep(ref_img, src_imgs, R_ref, p_ref, src_poses, K,
                                  stride=8, n_planes=40, keep_percentile=(5.0, 95.0),
                                  pad_frac=0.15):
    """Bound the fine sweep's depth range by first running a cheap, coarse
    sweep over the module's full MIN_DEPTH_M..MAX_DEPTH_M span and reading
    the answer off where the matches actually landed.

    This replaces relying on the sparse map for the range. The sparse map is
    triangulated from poses that are individually ~1.5 m uncertain over
    baselines of a few metres, and depth error there scales as
    Z * sigma_pose / baseline - measured on this dataset as a MEDIAN map
    point error of 18 m at ~85 m depth. A depth prior that wrong widens the
    fine sweep enough to undo the resolution it was supposed to buy.

    The coarse sweep has no such dependency: it is the same photometric
    matching the fine sweep does, just subsampled ~16x in pixels and ~2x in
    planes, so it costs a small fraction of one fine sweep and its answer
    degrades gracefully with pose error instead of being amplified by a
    short baseline. Returns None if too few pixels matched to trust it.

    The percentiles are trimmed rather than min/max because the coarse pass
    keeps a minority of confidently-wrong matches at the far ends of the
    search range, and a range set by those is wide enough to cost the fine
    pass exactly the resolution it was introduced to buy. Measured against
    ground truth here: 2nd/98th percentile gives 1.60 m median error,
    5th/95th gives 0.32 m for the same point count, and trimming harder
    (25th/75th) starts clipping real geometry and drops the yield tenfold.
    """
    depth_map, _cost, _u, _v = sweep_depth_map(
        ref_img, src_imgs, R_ref, p_ref, src_poses, K,
        depth_range=(MIN_DEPTH_M, MAX_DEPTH_M), stride=stride,
        n_planes_override=n_planes)
    d = depth_map[np.isfinite(depth_map)]
    if len(d) < 200:
        return None
    near, far = np.percentile(d, keep_percentile[0]), np.percentile(d, keep_percentile[1])
    span = max(far - near, 5.0)
    near = max(MIN_DEPTH_M, near - pad_frac * span)
    far = min(MAX_DEPTH_M, far + pad_frac * span)
    if far <= near * 1.02:
        return None
    return float(near), float(far)


def _plane_depths(near, far, max_baseline, fx, n_planes_override=None):
    """Inverse-depth-uniform plane sampling, with as many planes as it
    takes to keep consecutive planes within TARGET_DISPARITY_STEP_PX of
    each other for the widest baseline in play (disparity = fx*B/Z, so a
    step of delta in 1/Z is a step of fx*B*delta pixels)."""
    inv_near, inv_far = 1.0 / near, 1.0 / far
    inv_span = inv_near - inv_far
    if n_planes_override is not None:
        n = int(n_planes_override)
    else:
        step = TARGET_DISPARITY_STEP_PX / max(fx * max_baseline, 1e-6)
        n = int(np.ceil(inv_span / max(step, 1e-9))) + 1
        n = int(np.clip(n, MIN_PLANES, MAX_PLANES))
    inv_depths = np.linspace(inv_far, inv_near, n)   # ascending inverse depth
    return 1.0 / inv_depths, inv_depths              # depths descending, inv ascending


def _zncc_cost(ref, ref_mean, ref_var, warped, valid, window):
    """Windowed 1 - ZNCC between the reference and one warped source,
    computed with box filters (an integral-image sum in all but name).
    Pixels whose support window is not fully inside the source image are
    returned as NaN rather than a large cost, so they simply don't vote."""
    ksize = (window, window)
    warped = np.where(valid, warped, 0.0)
    valid_f = valid.astype(np.float32)

    frac_valid = cv2.boxFilter(valid_f, ddepth=-1, ksize=ksize)
    w_mean = cv2.boxFilter(warped, ddepth=-1, ksize=ksize)
    w_var = cv2.boxFilter(warped * warped, ddepth=-1, ksize=ksize) - w_mean * w_mean
    cov = cv2.boxFilter(ref * warped, ddepth=-1, ksize=ksize) - ref_mean * w_mean

    denom = np.sqrt(np.maximum(ref_var, 1e-6) * np.maximum(w_var, 1e-6))
    ncc = np.clip(cov / denom, -1.0, 1.0)
    cost = 1.0 - ncc
    return np.where(frac_valid >= MIN_VALID_FRACTION, cost, np.nan).astype(np.float32)


def sweep_depth_map(ref_img, src_imgs, R_ref, p_ref, src_poses, K,
                    depth_range=None, stride=2, window=COST_WINDOW,
                    n_planes_override=None):
    """Multi-view plane sweep. Returns (depth_map, cost_map, u_grid,
    v_grid) on the strided pixel grid; depth_map is NaN wherever no
    depth passed the accept/uniqueness/texture tests."""
    h, w = ref_img.shape[:2]
    gray_ref = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grays_src = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32) for im in src_imgs]

    us = np.arange(0, w, stride, dtype=np.float32)
    vs = np.arange(0, h, stride, dtype=np.float32)
    u_grid, v_grid = np.meshgrid(us, vs)
    flat_u, flat_v = u_grid.reshape(-1), v_grid.reshape(-1)

    # The reference is scored on the same strided grid the sweep runs on,
    # so its support window covers `window * stride` original pixels -
    # deliberate: it keeps the window's physical footprint (and therefore
    # the matching statistics) independent of the stride.
    ref = np.ascontiguousarray(gray_ref[::stride, ::stride])
    ksize = (window, window)
    ref_mean = cv2.boxFilter(ref, ddepth=-1, ksize=ksize)
    ref_var = cv2.boxFilter(ref * ref, ddepth=-1, ksize=ksize) - ref_mean * ref_mean
    textured = ref_var >= MIN_REF_VARIANCE

    near, far = depth_range if depth_range else (MIN_DEPTH_M, MAX_DEPTH_M)
    max_baseline = max(np.linalg.norm(sp[1] - p_ref) for sp in src_poses)
    depths, inv_depths = _plane_depths(near, far, max_baseline, K[0, 0],
                                       n_planes_override=n_planes_override)
    n_planes = len(depths)

    cost_vol = np.full((n_planes, ref.shape[0], ref.shape[1]), np.nan, dtype=np.float32)

    for di, d in enumerate(depths):
        Xw = _pixel_to_world_at_depth(flat_u, flat_v, np.full_like(flat_u, d),
                                      R_ref, p_ref, K)
        acc = np.zeros(ref.shape, dtype=np.float32)
        cnt = np.zeros(ref.shape, dtype=np.float32)
        for gray_src, (R_s, p_s) in zip(grays_src, src_poses):
            pix, depth_s = _world_to_pixels(Xw, R_s, p_s, K)
            map_x = pix[:, 0].reshape(ref.shape).astype(np.float32)
            map_y = pix[:, 1].reshape(ref.shape).astype(np.float32)
            warped = cv2.remap(gray_src, map_x, map_y, cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=np.nan)
            valid = (
                np.isfinite(warped)
                & (depth_s.reshape(ref.shape) > MIN_DEPTH_M)
                & (map_x >= 0) & (map_x < gray_src.shape[1] - 1)
                & (map_y >= 0) & (map_y < gray_src.shape[0] - 1)
            )
            cost = _zncc_cost(ref, ref_mean, ref_var, np.nan_to_num(warped), valid, window)
            ok = np.isfinite(cost)
            acc += np.where(ok, np.nan_to_num(cost), 0.0)
            cnt += ok
        cost_vol[di] = np.where(cnt > 0, acc / np.maximum(cnt, 1.0), np.nan)

    d_inv_step = inv_depths[1] - inv_depths[0]
    disparity_per_plane = K[0, 0] * max_baseline * d_inv_step
    return _extract_depth(cost_vol, inv_depths, textured,
                          disparity_per_plane) + (u_grid, v_grid)


def _extract_depth(cost_vol, inv_depths, textured, disparity_per_plane):
    """Winner-take-all over the cost volume, plus the three tests that
    separate a real match from a confident-looking accident, plus
    sub-plane parabolic refinement of the winner in inverse-depth space."""
    n_planes = cost_vol.shape[0]
    filled = np.where(np.isfinite(cost_vol), cost_vol, np.inf)
    best_idx = np.argmin(filled, axis=0)
    ii, jj = np.indices(best_idx.shape)
    best_cost = filled[best_idx, ii, jj]

    # Runner-up outside the winner's own basin, so a broad single minimum
    # isn't mistaken for two competing ones. The basin's width is set in
    # disparity pixels and converted to planes here.
    exclude = max(1, int(np.ceil(UNIQUENESS_EXCLUDE_PX / max(disparity_per_plane, 1e-6))))
    plane_idx = np.arange(n_planes)[:, None, None]
    outside = np.abs(plane_idx - best_idx[None, :, :]) > exclude
    second_cost = np.min(np.where(outside, filled, np.inf), axis=0)

    with np.errstate(invalid="ignore"):
        good = (
            np.isfinite(best_cost)
            & (best_cost < MAX_ACCEPT_COST)
            & (second_cost - best_cost >= UNIQUENESS_MARGIN)
            & textured
            & (best_idx > 0) & (best_idx < n_planes - 1)   # refinable interior winner
        )

    k = np.clip(best_idx, 1, n_planes - 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        c0 = np.nan_to_num(filled[k - 1, ii, jj], nan=0.0, posinf=0.0)
        c1 = np.nan_to_num(filled[k, ii, jj], nan=0.0, posinf=0.0)
        c2 = np.nan_to_num(filled[k + 1, ii, jj], nan=0.0, posinf=0.0)
        denom = c0 - 2 * c1 + c2
        offset = np.where(np.abs(denom) > 1e-9,
                          0.5 * (c0 - c2) / np.where(np.abs(denom) > 1e-9, denom, 1.0), 0.0)
    offset = np.clip(np.nan_to_num(offset), -0.5, 0.5)

    d_inv_step = inv_depths[1] - inv_depths[0]
    inv_best = inv_depths[k] + offset * d_inv_step
    depth_map = np.where(good & (inv_best > 0), 1.0 / np.where(inv_best > 0, inv_best, 1.0), np.nan)
    cost_map = np.where(good, best_cost, np.nan)
    return depth_map.astype(np.float32), cost_map.astype(np.float32)


def stereo_pair_to_points(img1, img2, R1, p1, R2, p2, K, stride=2, depth_range=None):
    """Two-view convenience wrapper around sweep_depth_map, kept as the
    single-pair entry point used by tests and by callers that only have
    one partner frame."""
    return stereo_views_to_points(img1, [img2], R1, p1, [(R2, p2)], K,
                                  stride=stride, depth_range=depth_range)


def stereo_views_to_points(ref_img, src_imgs, R_ref, p_ref, src_poses, K,
                           stride=2, depth_range=None):
    """Runs the sweep and lifts the surviving pixels to colored world
    points."""
    depth_map, _cost, u_grid, v_grid = sweep_depth_map(
        ref_img, src_imgs, R_ref, p_ref, src_poses, K,
        depth_range=depth_range, stride=stride)

    vi, ui = np.where(np.isfinite(depth_map))
    if len(ui) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3))

    sel_u = u_grid[vi, ui].astype(np.float64)
    sel_v = v_grid[vi, ui].astype(np.float64)
    sel_depth = depth_map[vi, ui].astype(np.float64)
    pts_world = _pixel_to_world_at_depth(sel_u, sel_v, sel_depth, R_ref, p_ref, K)

    colors = ref_img[np.round(sel_v).astype(np.int64),
                     np.round(sel_u).astype(np.int64), :].astype(np.float64)[:, ::-1]  # BGR->RGB
    return pts_world, colors


def build_dense_cloud(frames, poses, K, keyframe_stride=KEYFRAME_STRIDE,
                      blurred=None, sparse_points=None, stride=2, progress_cb=None):
    """frames: list of BGR images. poses: index-aligned (R, p) world poses
    (already georeferenced-consistent, i.e. all in the same frame the
    caller wants the output cloud in - typically local ENU, converted to
    UTM afterwards by the caller). blurred: optional bool array, frames
    flagged by ingest.py's sharpness score are skipped as reference or
    source frames (garbage in, garbage out). sparse_points: tracking.py's
    triangulated map points, used only to bound each keyframe's depth
    sweep - see depth_range_from_sparse.
    """
    all_points, all_colors = [], []
    n = len(frames)
    for i in range(0, n, keyframe_stride):
        if blurred is not None and blurred[i]:
            continue
        partners = [j for j in find_stereo_partners(poses, i)
                    if blurred is None or not blurred[j]]
        if not partners:
            continue
        R_ref, p_ref = poses[i]
        src_imgs = [frames[j] for j in partners]
        src_poses = [poses[j] for j in partners]
        # Coarse-to-fine: a cheap full-range sweep decides where the scene
        # actually is, then the expensive sweep spends all its planes there.
        # The sparse map is only a fallback - see the two depth_range_*
        # functions for why the coarse sweep is the more trustworthy of them.
        depth_range = depth_range_from_coarse_sweep(
            frames[i], src_imgs, R_ref, p_ref, src_poses, K)
        if depth_range is None:
            depth_range = depth_range_from_sparse(sparse_points, R_ref, p_ref, K, frames[i].shape)
        pts, cols = stereo_views_to_points(
            frames[i], src_imgs, R_ref, p_ref, src_poses, K,
            stride=stride, depth_range=depth_range)
        if len(pts):
            all_points.append(pts)
            all_colors.append(cols)
        if progress_cb:
            progress_cb(i, n, len(pts))

    if not all_points:
        return np.zeros((0, 3)), np.zeros((0, 3))
    return np.vstack(all_points), np.vstack(all_colors)


def voxel_downsample(points, colors, voxel_size):
    """Average points/colors that fall in the same voxel cell. Vectorized
    (no external library) via np.unique on integer cell indices."""
    if len(points) == 0:
        return points, colors
    keys = np.floor(points / voxel_size).astype(np.int64)
    _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)

    n_cells = counts.shape[0]
    sum_pts = np.zeros((n_cells, 3))
    sum_cols = np.zeros((n_cells, 3))
    np.add.at(sum_pts, inverse, points)
    np.add.at(sum_cols, inverse, colors)
    mean_pts = sum_pts / counts[:, None]
    mean_cols = sum_cols / counts[:, None]
    return mean_pts, mean_cols


def statistical_outlier_removal(points, colors, k=8, std_ratio=2.0):
    """Same criterion Open3D's remove_statistical_outlier uses: reject
    points whose mean distance to their k nearest neighbors is more than
    std_ratio standard deviations above the global mean - hand-rolled
    with scipy's cKDTree since Open3D itself isn't installable here."""
    if len(points) < k + 1:
        return points, colors
    tree = cKDTree(points)
    dists, _ = tree.query(points, k=k + 1)  # includes the point itself at dist 0
    mean_dists = dists[:, 1:].mean(axis=1)
    global_mean, global_std = mean_dists.mean(), mean_dists.std()
    keep = mean_dists < global_mean + std_ratio * global_std
    return points[keep], colors[keep]
