"""
GPS-anchoring for a monocular, relative-scale trajectory (LingBot-Map's, or
any other feed-forward model's) via a similarity transform.

WHY THIS EXISTS

LingBot-Map (like every monocular feed-forward reconstruction model - DUSt3R/
MASt3R/VGGT-lineage included) has no metric scale and no fixed world frame:
its camera positions are self-consistent in *some* arbitrary units, with an
arbitrary origin and orientation, chosen by the first frame. That's the exact
same scale-ambiguity problem rtvio's own monocular VO already has (see
rtvio/README.md "Pose comes from vision, not IMU") - nothing about switching
models removes it.

The fix isn't a Kalman filter and isn't a per-frame GPS snap (what rtvio's
old live_pipeline.py did): those correct a trajectory that's already in the
right units. Here the trajectory isn't even in the right *units* yet - one
GPS fix can't fix a global scale/rotation ambiguity, only a similarity
transform fit over several fixes can. This is the standard way monocular VO/
SLAM output gets geo-registered for evaluation (e.g. the TUM RGB-D benchmark's
`evaluate_ate.py`), applied here as the actual production step rather than a
scoring-time-only tool.

THE MATH

Given N corresponding point pairs (model-frame position, GPS-derived ENU
position), find scale s, rotation R, translation t minimizing
    sum_i || s * R @ model_i + t - enu_i ||^2
closed-form, via Umeyama (1991) / Horn's method (1987) - same problem, same
answer. No iteration, no initial guess, exact for N >= 3 non-collinear points
(more points -> more robust to per-fix GPS noise).

Apply the SAME (s, R, t) to every model-frame point and pose - not just the
ones with a matching GPS fix - to bring the whole reconstruction (dense cloud
included) into local ENU metres, consistent with rtvio's
`ref_lat_deg/ref_lon_deg/ref_alt_m` origin. From there, rtvio's existing
`meshing.py` / `export.py` / `georeference.py` need no changes at all - they
already only know how to consume local-ENU-metre points and poses.
"""
import numpy as np


def umeyama_alignment(src, dst, with_scale=True):
    """Closed-form similarity transform mapping `src` onto `dst`.

    Args:
        src: (N, 3) points in the source frame (here: LingBot-Map's arbitrary
            units/frame).
        dst: (N, 3) corresponding points in the destination frame (here: GPS
            ENU metres, about rtvio's fixed reference origin).
        with_scale: fit a scale factor too. Always True for us - a monocular
            model has no metric scale to begin with, so leaving this False
            would silently keep whatever arbitrary scale the model picked.

    Returns:
        R: (3, 3) rotation. t: (3,) translation. s: float scale, such that
        `s * (R @ src[i]) + t` approximates `dst[i]` for every i.

    Raises:
        ValueError: fewer than 3 points, or the points are (near-)collinear -
        neither can constrain a 3D rotation, and a transform "fit" to them
        would be numerically meaningless rather than merely imprecise.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 3:
        raise ValueError("src/dst must both be (N, 3) with matching N; got "
                          "%r and %r" % (src.shape, dst.shape))
    n = src.shape[0]
    if n < 3:
        raise ValueError(
            "need >= 3 point correspondences to fit a 3D similarity "
            "transform (rotation has 3 DOF); got %d. In practice this means "
            "fewer than 3 GPS fixes arrived during the flight - nothing "
            "downstream can be georeferenced yet." % n)

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean

    # Umeyama 1991, eq. 34-43. Also exactly Horn's 1987 closed-form solution
    # to the same least-squares problem.
    cov = (dst_c.T @ src_c) / n
    U, D, Vt = np.linalg.svd(cov)

    rank = np.sum(D > 1e-8 * D.max() if D.max() > 0 else D > 1e-8)
    if rank < 2:
        raise ValueError(
            "GPS fixes used for alignment are (near-)collinear (covariance "
            "rank %d < 2) - a straight-line flight path cannot constrain a "
            "3D rotation. Wait for more fixes off that line (e.g. a turn), "
            "or accept an unconstrained yaw the way rtvio's live path "
            "already documents for --indoor sessions." % rank)

    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1.0  # reflection correction, keeps R a proper rotation

    R = U @ S @ Vt

    if with_scale:
        src_var = (src_c ** 2).sum(axis=1).mean()
        if src_var < 1e-12:
            raise ValueError(
                "source points have ~zero spread (all corresponding model "
                "positions coincide) - cannot recover a scale factor from "
                "them.")
        s = float(np.trace(np.diag(D) @ S) / src_var)
    else:
        s = 1.0

    t = dst_mean - s * (R @ src_mean)
    return R, t, s


def apply_similarity(points, R, t, s):
    """Apply `s * R @ p + t` to every row of `points` (N, 3). Also correct
    for poses: apply the same transform to camera centres; camera
    orientations only need the rotation `R` composed in (translation/scale
    don't affect orientation)."""
    points = np.asarray(points, dtype=np.float64)
    return (s * (points @ R.T)) + t


def align_trajectory_to_gps(model_positions, model_timestamps,
                             gps_enu_positions, gps_timestamps,
                             max_time_diff_s=0.5):
    """Fit the model-frame -> local-ENU similarity transform from GPS fixes,
    matching each GPS fix to its nearest model-trajectory timestamp.

    Args:
        model_positions: (M, 3) LingBot-Map (or any monocular model's) camera
            positions, arbitrary scale/frame, one per processed frame.
        model_timestamps: (M,) seconds, same clock as gps_timestamps (see
            INTEGRATION.md section 4.1 - get this right first, or the
            matching below silently pairs the wrong frame with each fix).
        gps_enu_positions: (K, 3) local ENU metres from
            rtvio.stream.geodesy.latlon_to_enu, about rtvio's fixed
            reference origin.
        gps_timestamps: (K,) seconds, same clock as model_timestamps.
        max_time_diff_s: reject a GPS fix if no model frame exists within
            this many seconds of it - pairing a fix with a frame half a
            second away silently injects position error into the fit (the
            platform has moved in that gap), exactly the failure mode
            INTEGRATION.md section 4.2 already warns about for frame timing.

    Returns:
        (R, t, s, used_pairs) - the fitted transform (see umeyama_alignment)
        and the number of GPS fixes actually used after the time-gate above
        (report this - a fit from 3 barely-passing fixes is far less trustworthy
        than one from 30, even though both "succeed").
    """
    model_positions = np.asarray(model_positions, dtype=np.float64)
    model_timestamps = np.asarray(model_timestamps, dtype=np.float64)
    gps_enu_positions = np.asarray(gps_enu_positions, dtype=np.float64)
    gps_timestamps = np.asarray(gps_timestamps, dtype=np.float64)

    src, dst = [], []
    for gps_t, gps_p in zip(gps_timestamps, gps_enu_positions):
        j = int(np.argmin(np.abs(model_timestamps - gps_t)))
        if abs(model_timestamps[j] - gps_t) <= max_time_diff_s:
            src.append(model_positions[j])
            dst.append(gps_p)

    if len(src) < 3:
        raise ValueError(
            "only %d GPS fix(es) matched a model frame within %.2fs - need "
            ">= 3 to fit a similarity transform. Either GPS hasn't produced "
            "enough fixes yet this session, or the two timestamp streams "
            "aren't on the same clock (INTEGRATION.md section 4.1)."
            % (len(src), max_time_diff_s))

    R, t, s = umeyama_alignment(np.array(src), np.array(dst))
    return R, t, s, len(src)


if __name__ == "__main__":
    # Self-test: build a known similarity transform, apply it, recover it,
    # and check we get the original back to float precision - the same
    # "pin the convention with an assertion that fails loudly" ethos as
    # rtvio/tests/test_geometry.py.
    rng = np.random.default_rng(0)

    true_scale = 3.7
    axis = np.array([0.2, -0.5, 0.8])
    axis = axis / np.linalg.norm(axis)
    angle = 0.6
    K = np.array([[0, -axis[2], axis[1]],
                  [axis[2], 0, -axis[0]],
                  [-axis[1], axis[0], 0]])
    true_R = np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * (K @ K)
    true_t = np.array([120.0, -45.0, 8.0])

    n = 40
    src_pts = rng.normal(size=(n, 3)) * 5.0
    dst_pts = apply_similarity(src_pts, true_R, true_t, true_scale)

    est_R, est_t, est_s = umeyama_alignment(src_pts, dst_pts)

    assert np.allclose(est_R, true_R, atol=1e-8), "rotation not recovered"
    assert np.allclose(est_t, true_t, atol=1e-6), "translation not recovered"
    assert abs(est_s - true_scale) < 1e-8, "scale not recovered"
    print("umeyama_alignment: recovered scale=%.6f (true %.6f), "
          "rotation/translation match to float precision" % (est_s, true_scale))

    # Same check through the timestamp-matching entry point, with noisy GPS
    # (this is the realistic path: GPS fixes don't land exactly on a model
    # frame's timestamp, and carry a few metres of noise).
    model_t = np.linspace(0, 20, 200)
    model_p = rng.normal(size=(200, 3)) * 5.0
    gt_enu = apply_similarity(model_p, true_R, true_t, true_scale)
    gps_t = np.arange(0, 20, 1.0) + rng.normal(scale=0.05, size=20)  # ~1 Hz, jittered
    nearest = np.array([np.argmin(np.abs(model_t - t)) for t in gps_t])
    gps_enu = gt_enu[nearest] + rng.normal(scale=2.0, size=(len(gps_t), 3))  # 2m GPS noise

    R2, t2, s2, used = align_trajectory_to_gps(model_p, model_t, gps_enu, gps_t)
    recovered = apply_similarity(model_p, R2, t2, s2)
    err = np.linalg.norm(recovered - gt_enu, axis=1)
    print("align_trajectory_to_gps: used %d/%d fixes, scale=%.3f (true %.3f), "
          "median position error vs noise-free ground truth = %.3f m"
          % (used, len(gps_t), s2, true_scale, np.median(err)))
    assert used == len(gps_t)
    # Not < 1.0m: a 7-DOF fit (3 rotation + 3 translation + 1 scale) over 20
    # correspondences each carrying 2m of iid noise doesn't average that noise
    # down to sub-metre - this bound is the input noise scale itself, not a
    # cherry-picked pass. What matters is that it's bounded and doesn't blow
    # up, not that it beats the input noise.
    assert np.median(err) < 2.5, "alignment error should stay commensurate with GPS noise"
    print("OK")
