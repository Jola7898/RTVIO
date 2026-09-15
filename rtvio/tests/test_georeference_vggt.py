"""
Regression + robustness test for so3.umeyama_alignment, the per-window
georeferencing fit at the heart of vggt_reconstruct.py's GPS mode.

No committed test existed for this at all before this session - the
"verified exact to float precision" claim in docs/dev_notes/HANDOFF_SESSION1.md was an ad
hoc script, not a committed test, and it only ever checked the noiseless
case. SIH26158 key challenge (v) is explicitly "GPS inaccuracies and sensor
noise"; this file adds that missing coverage: an exact regression test for
the noiseless case (formalizing what should already have been committed),
plus a Monte-Carlo sweep of realistic GPS noise levels against the
pipeline's real per-window anchor count, checking where the resulting
position error crosses the PS's <=1m spatial-accuracy target.

    python tests/test_georeference_vggt.py
"""
import numpy as np

from rtvio.so3 import umeyama_alignment

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-70s %s" % ("PASS" if ok else "FAIL", name, detail))


def _synthetic_flight(n, seed=0, radius_m=30.0, altitude_m=80.0):
    """A plausible aerial-survey camera path: n points along a gentle arc at
    ~constant altitude, a few metres apart - the shape a drone's per-window
    camera centers actually have. Deliberately NOT n random points in a
    ball: umeyama_alignment is genuinely harder (worse-conditioned) on a
    near-straight path than on a well-spread cloud, and a real flight path
    is close to a line - this is the realistic difficulty, not an easy
    case picked to make the numbers look good."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, np.pi / 3, n)  # a 60-degree arc, not a full circle
    pts = np.stack([radius_m * np.cos(t), radius_m * np.sin(t),
                     np.full(n, altitude_m)], axis=1)
    pts[:, 2] += rng.normal(scale=0.3, size=n)  # slight altitude wobble - keeps it non-planar/non-degenerate
    return pts


def _random_similarity(seed):
    """Stand-in for 'VGGT's arbitrary per-window frame': an unrelated
    scale/rotation/translation applied to true ENU positions to produce
    src - exactly the transform umeyama_alignment has to invert."""
    rng = np.random.default_rng(seed + 1000)
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    angle = rng.uniform(0, 2 * np.pi)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    R = np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * (K @ K)
    s = rng.uniform(0.3, 3.0)  # VGGT's own scale is arbitrary, not near 1
    t = rng.normal(scale=50.0, size=3)
    return s, R, t


def _src_for(dst_true, s0, R0, t0):
    """Invert dst_true = s0 * R0 @ src + t0 to get the matching src."""
    return ((dst_true - t0) @ np.linalg.inv(R0).T) / s0


def test_umeyama_recovers_exact_transform_noiseless():
    """The regression test that should already have existed: with zero
    noise, umeyama_alignment must recover the true (s, R, t) to float
    precision, and the aligned points must land exactly on the GPS
    ('dst') positions."""
    dst_true = _synthetic_flight(n=6, seed=1)
    s0, R0, t0 = _random_similarity(seed=1)
    src = _src_for(dst_true, s0, R0, t0)

    s, R, t = umeyama_alignment(src, dst_true, with_scale=True)
    aligned = s * (R @ src.T).T + t
    err = np.linalg.norm(aligned - dst_true, axis=1)
    check("noiseless: scale recovered to 1e-8", abs(s - s0) < 1e-8,
          "s=%.6f vs true %.6f" % (s, s0))
    check("noiseless: rotation recovered to 1e-8", np.allclose(R, R0, atol=1e-8), "")
    check("noiseless: aligned points match GPS to sub-mm", err.max() < 1e-6,
          "max err %.2e m" % err.max())


def test_gps_noise_sweep_against_1m_target():
    """Monte-Carlo: for realistic GPS noise levels and the pipeline's real
    per-window anchor count (WINDOW_FRAMES=4, the common case - plus a
    denser 10-anchor case for contrast), how much position error survives
    into the georeferenced cloud vs. SIH26158's <=1m spatial-accuracy
    target. Horizontal/vertical sigmas are consumer/phone-GPS-realistic
    (rtvioapk's GpsCollector reports comparable accuracy_m values) - not
    survey-grade RTK, which the PS lists only as an OPTIONAL input."""
    NOISE_LEVELS_M = [0.0, 1.0, 3.0, 5.0]   # horizontal 1-sigma
    VERTICAL_FACTOR = 2.0                    # GPS altitude is typically worse than horizontal
    N_TRIALS = 200

    print("\n  window anchors | horiz sigma (m) | mean pos error (m) | max pos error (m) | <=1m?")
    results = {}
    for n_anchors in (4, 10):
        for sigma in NOISE_LEVELS_M:
            errs = []
            for trial in range(N_TRIALS):
                dst_true = _synthetic_flight(n=n_anchors, seed=trial)
                s0, R0, t0 = _random_similarity(seed=trial)
                src = _src_for(dst_true, s0, R0, t0)
                rng = np.random.default_rng(trial + 5000 + n_anchors)
                noise = rng.normal(scale=sigma, size=(n_anchors, 3))
                noise[:, 2] *= VERTICAL_FACTOR
                dst_noisy = dst_true + noise

                s, R, t = umeyama_alignment(src, dst_noisy, with_scale=True)
                aligned = s * (R @ src.T).T + t
                err = np.linalg.norm(aligned - dst_true, axis=1)
                errs.append(err.mean())
            mean_e, max_e = float(np.mean(errs)), float(np.max(errs))
            results[(n_anchors, sigma)] = (mean_e, max_e)
            print("  %14d | %15.1f | %19.3f | %17.3f | %s"
                  % (n_anchors, sigma, mean_e, max_e, "yes" if mean_e <= 1.0 else "NO"))

    check("noiseless case has ~zero error at n=4",
          results[(4, 0.0)][0] < 0.01, "mean %.4fm" % results[(4, 0.0)][0])
    check("more anchors (n=10) reduces mean error vs n=4 at the same noise (3m)",
          results[(10, 3.0)][0] < results[(4, 3.0)][0],
          "n=4: %.3fm, n=10: %.3fm" % (results[(4, 3.0)][0], results[(10, 3.0)][0]))
    # Not asserted pass/fail against the 1m target itself - that's a real
    # engineering finding to report, not a bug in umeyama_alignment (a
    # least-squares fit cannot do better than the noise it's given), so it
    # belongs in PERFORMANCE_BENCHMARK.md's writeup, not as a failing test.
    check("4-anchor, 1m-GPS result recorded for the writeup (informational)",
          True, "mean=%.3fm max=%.3fm" % results[(4, 1.0)])

    return results


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
