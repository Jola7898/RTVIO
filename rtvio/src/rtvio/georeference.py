"""
Local ENU (flight-relative metres, what the EKF/tracker work in) -> real
UTM easting/northing, without pyproj - pyproj's latest release has no
Windows wheel for this machine's Python 3.13 (verified against PyPI
directly during planning). The Transverse Mercator forward projection
below is the standard closed-form (Snyder 1987) algorithm used by most
from-scratch UTM converters; it's WGS84-only and accurate to a few
millimetres well away from the poles, which is all a synthetic single
UTM-zone flight needs.

Also owns the frozen pose-file schema from rtvio_3/PROJECT_CONTEXT.md
section 4.1: one row per camera/frame, read by dense_stereo.py/meshing.py,
written here.
"""
import csv
import math

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
K0 = 0.9996

POSE_FILE_COLUMNS = ["timestamp", "X_utm", "Y_utm", "Z", "qw", "qx", "qy", "qz", "fx", "fy", "cx", "cy", "epsg"]


def enu_to_latlon(dx, dy, dz, ref_lat_deg, ref_lon_deg, ref_alt_m):
    """Local flat-Earth approximation - valid because our synthetic scene
    spans only ~300m, many orders of magnitude below the ~10km scale
    where Earth curvature would start to matter for this approximation."""
    earth_r = 6378137.0
    ref_lat_rad = math.radians(ref_lat_deg)
    dlat_deg = (dy / earth_r) * (180.0 / math.pi)
    dlon_deg = (dx / (earth_r * math.cos(ref_lat_rad))) * (180.0 / math.pi)
    return ref_lat_deg + dlat_deg, ref_lon_deg + dlon_deg, ref_alt_m + dz


def utm_zone_number(lon_deg):
    return int(math.floor((lon_deg + 180.0) / 6.0)) + 1


def latlon_to_utm(lat_deg, lon_deg):
    """Returns (easting_m, northing_m, zone_number, epsg_code)."""
    a, f = WGS84_A, WGS84_F
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)

    zone = utm_zone_number(lon_deg)
    lon0_deg = (zone - 1) * 6 - 180 + 3
    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)
    lam0 = math.radians(lon0_deg)

    sin_phi, cos_phi, tan_phi = math.sin(phi), math.cos(phi), math.tan(phi)
    N = a / math.sqrt(1 - e2 * sin_phi ** 2)
    T = tan_phi ** 2
    C = ep2 * cos_phi ** 2
    A = cos_phi * (lam - lam0)

    M = a * (
        (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
        + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
        - (35 * e2 ** 3 / 3072) * math.sin(6 * phi)
    )

    easting = K0 * N * (
        A + (1 - T + C) * A ** 3 / 6
        + (5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * A ** 5 / 120
    ) + 500000.0

    northing = K0 * (
        M + N * tan_phi * (
            A ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24
            + (61 - 58 * T + T ** 2 + 600 * C - 330 * ep2) * A ** 6 / 720
        )
    )
    if lat_deg < 0:
        northing += 10_000_000.0

    epsg = 32600 + zone if lat_deg >= 0 else 32700 + zone
    return easting, northing, zone, epsg


def enu_to_utm(dx, dy, dz, ref_lat_deg, ref_lon_deg, ref_alt_m):
    lat, lon, alt = enu_to_latlon(dx, dy, dz, ref_lat_deg, ref_lon_deg, ref_alt_m)
    easting, northing, zone, epsg = latlon_to_utm(lat, lon)
    return easting, northing, alt, epsg


def enu_to_utm_array(enu_xyz, ref_lat_deg, ref_lon_deg, ref_alt_m):
    """Vectorized enu_to_utm for a whole point cloud. Same closed-form
    Snyder projection as latlon_to_utm, with numpy in place of math, and
    two deliberate differences from calling enu_to_utm in a loop:

    - the UTM zone (and therefore the central meridian) is fixed once from
      the REFERENCE longitude rather than recomputed per point. A cloud
      that straddled a zone boundary would otherwise be written with two
      different projections mixed into one file, which no reader can
      interpret; pinning the zone is the standard behaviour and is what
      the accompanying single .prj/EPSG code already claims.
    - it is roughly two orders of magnitude faster, which matters because
      this runs once per point over a multi-million-point cloud.

    Returns (Nx3 array of easting/northing/altitude, epsg).
    """
    import numpy as np

    enu = np.asarray(enu_xyz, dtype=np.float64)
    earth_r = 6378137.0
    ref_lat_rad = math.radians(ref_lat_deg)
    lat_deg = ref_lat_deg + (enu[:, 1] / earth_r) * (180.0 / math.pi)
    lon_deg = ref_lon_deg + (enu[:, 0] / (earth_r * math.cos(ref_lat_rad))) * (180.0 / math.pi)
    alt = ref_alt_m + enu[:, 2]

    a, f = WGS84_A, WGS84_F
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)

    zone = utm_zone_number(ref_lon_deg)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi = np.radians(lat_deg)
    lam = np.radians(lon_deg)

    sin_phi, cos_phi, tan_phi = np.sin(phi), np.cos(phi), np.tan(phi)
    N = a / np.sqrt(1 - e2 * sin_phi ** 2)
    T = tan_phi ** 2
    C = ep2 * cos_phi ** 2
    A = cos_phi * (lam - lon0)

    M = a * (
        (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * np.sin(2 * phi)
        + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * np.sin(4 * phi)
        - (35 * e2 ** 3 / 3072) * np.sin(6 * phi)
    )

    easting = K0 * N * (
        A + (1 - T + C) * A ** 3 / 6
        + (5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * A ** 5 / 120
    ) + 500000.0
    northing = K0 * (
        M + N * tan_phi * (
            A ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24
            + (61 - 58 * T + T ** 2 + 600 * C - 330 * ep2) * A ** 6 / 720
        )
    )
    if ref_lat_deg < 0:
        northing = northing + 10_000_000.0
    epsg = (32600 if ref_lat_deg >= 0 else 32700) + zone
    return np.column_stack([easting, northing, alt]), epsg


def quat_wxyz_from_R(R):
    """Rotation matrix -> scalar-first quaternion (qw,qx,qy,qz), matching
    the frozen pose-file schema's convention."""
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        S = math.sqrt(tr + 1.0) * 2
        qw = 0.25 * S
        qx = (R[2, 1] - R[1, 2]) / S
        qy = (R[0, 2] - R[2, 0]) / S
        qz = (R[1, 0] - R[0, 1]) / S
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        S = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        qw = (R[2, 1] - R[1, 2]) / S
        qx = 0.25 * S
        qy = (R[0, 1] + R[1, 0]) / S
        qz = (R[0, 2] + R[2, 0]) / S
    elif R[1, 1] > R[2, 2]:
        S = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        qw = (R[0, 2] - R[2, 0]) / S
        qx = (R[0, 1] + R[1, 0]) / S
        qy = 0.25 * S
        qz = (R[1, 2] + R[2, 1]) / S
    else:
        S = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        qw = (R[1, 0] - R[0, 1]) / S
        qx = (R[0, 2] + R[2, 0]) / S
        qy = (R[1, 2] + R[2, 1]) / S
        qz = 0.25 * S
    return qw, qx, qy, qz


def write_pose_file(path, rows):
    """rows: list of dicts with POSE_FILE_COLUMNS keys."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=POSE_FILE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_pose_file(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def georeference_trajectory(timestamps, positions_enu, rotations, ref_lat_deg, ref_lon_deg, ref_alt_m, K):
    """positions_enu: list of 3-vectors (local ENU, metres). rotations:
    list of 3x3 body-to-world matrices. Returns pose-file rows (see
    POSE_FILE_COLUMNS) plus the resolved epsg for convenience."""
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    rows = []
    epsg = None
    for t, p, R in zip(timestamps, positions_enu, rotations):
        X, Y, Z, epsg = enu_to_utm(p[0], p[1], p[2], ref_lat_deg, ref_lon_deg, ref_alt_m)
        qw, qx, qy, qz = quat_wxyz_from_R(R)
        rows.append({
            "timestamp": t, "X_utm": X, "Y_utm": Y, "Z": Z,
            "qw": qw, "qx": qx, "qy": qy, "qz": qz,
            "fx": fx, "fy": fy, "cx": cx, "cy": cy, "epsg": epsg,
        })
    return rows, epsg


def course_over_ground(positions_enu, min_distance_m=5.0):
    """Heading (radians, 0 = east, CCW positive) of the vehicle's track,
    from a short run of position fixes. Returns None if the vehicle has
    not moved far enough for the direction to mean anything - over a short
    distance GPS noise dominates the displacement and the "heading" is
    just noise.

    This is a yaw observation, which is the one component of attitude that
    neither the accelerometer nor GPS position alone can provide; see
    so3.level_and_align_attitude.
    """
    import numpy as np
    P = np.asarray(positions_enu, dtype=np.float64)
    if len(P) < 2:
        return None
    d = P[-1][:2] - P[0][:2]
    if np.linalg.norm(d) < min_distance_m:
        return None
    return float(np.arctan2(d[1], d[0]))


def velocity_from_fixes(timestamps, positions_enu, min_span_s=0.4):
    """Initial velocity estimate by least-squares line fit through a short
    run of position fixes.

    Seeding the filter with zero velocity instead is not a neutral choice
    for an aircraft that is already flying: on this dataset the UAV is
    doing 10 m/s from the first sample, which is a 5-sigma error against
    the filter's own 2 m/s velocity prior. A Kalman filter cannot report
    "my prior was wrong" - it distributes the resulting innovations across
    whichever states are correlated with velocity, which here means
    attitude and the IMU biases, and those corruptions do not undo
    themselves once the velocity has settled.

    A line fit rather than a first-difference because GPS position noise
    (metres) divided by a short interval is a large velocity error; fitting
    several fixes averages it down.
    """
    import numpy as np
    t = np.asarray(timestamps, dtype=np.float64)
    P = np.asarray(positions_enu, dtype=np.float64)
    if len(t) < 2 or (t[-1] - t[0]) < min_span_s:
        return None
    A = np.column_stack([t - t[0], np.ones(len(t))])
    coeffs, *_ = np.linalg.lstsq(A, P, rcond=None)
    return coeffs[0]   # the slope row: d(position)/dt
