"""
lat/lon/alt -> local ENU metres, for the GPS packets the EKF consumes.

`ekf.update_gps` takes ENU metres about a fixed origin, and that origin
must be the SAME one live_pipeline.py writes to session_config.json and
hands to georeference_trajectory and export_las. Get the two inconsistent
and the model is internally fine and georeferenced to the wrong place - a
failure with no symptom until someone opens the LAS in QGIS.

CONVENTION: this is the exact algebraic inverse of
georeference.enu_to_latlon, not a better projection.

That is a deliberate choice and worth defending. enu_to_latlon is a
flat-Earth approximation on a sphere of radius WGS84_A; a rigorous
WGS84 ellipsoidal ENU (what pyproj would give) disagrees with it by
roughly 0.3% in northing at this latitude, which over a 300 m scene is
~1 m. Introducing that as a SECOND convention would mean the position the
EKF fuses and the position georeference.py projects to UTM disagree by a
metre, silently, forever. One self-consistent approximation beats two
mutually inconsistent accurate-looking ones.

If the flat-Earth error ever needs to go away, replace BOTH directions
together and re-run the round-trip test in tests/test_stream.py.

ALTITUDE is the field most likely to be quietly wrong. The app documents
`altitude_m` as metres above the WGS84 ellipsoid, but Android vendor
implementations vary and some return orthometric (geoid) height instead.
At Bengaluru the geoid separation is about -84 m, so the wrong one gives a
systematic Z offset of that size with no other symptom. Verify against a
known elevation before trusting Z; `AltitudeSanity` below is the cheapest
version of that check.
"""
import math

EARTH_R_M = 6378137.0     # must match georeference.enu_to_latlon exactly


def latlon_to_enu(lat_deg, lon_deg, alt_m, ref_lat_deg, ref_lon_deg, ref_alt_m):
    """Returns (east_m, north_m, up_m) relative to the reference origin."""
    ref_lat_rad = math.radians(ref_lat_deg)
    north = math.radians(lat_deg - ref_lat_deg) * EARTH_R_M
    east = math.radians(lon_deg - ref_lon_deg) * EARTH_R_M * math.cos(ref_lat_rad)
    return east, north, alt_m - ref_alt_m


def enu_to_latlon(east_m, north_m, up_m, ref_lat_deg, ref_lon_deg, ref_alt_m):
    """Mirror of georeference.enu_to_latlon, re-exported here so the
    round-trip test can exercise both directions from one import."""
    ref_lat_rad = math.radians(ref_lat_deg)
    lat = ref_lat_deg + math.degrees(north_m / EARTH_R_M)
    lon = ref_lon_deg + math.degrees(east_m / (EARTH_R_M * math.cos(ref_lat_rad)))
    return lat, lon, ref_alt_m + up_m


# GPS accuracy handling ------------------------------------------------------
#
# The app streams every fix it is given, whether accuracy is 3 m or 50 m, so a
# single global config["gps_noise_std_m"] either trusts the bad fixes or
# distrusts the good ones. ekf.update_gps already accepts a per-call sigma, so
# using the per-fix value costs no filter change at all.

DEFAULT_GPS_SIGMA_M = 8.0   # used when accuracy_m is -1 (unknown). Deliberately
                            # pessimistic: an unknown-accuracy fix is usually an
                            # early one taken before the receiver has settled.
MIN_GPS_SIGMA_M = 1.0       # phones report optimistic accuracy; never let one
                            # fix dominate the filter
MAX_GPS_SIGMA_M = 50.0      # past this the fix carries no information, but
                            # clamping keeps it from destabilising the update
VERTICAL_SIGMA_FACTOR = 2.5  # GNSS vertical error is consistently worse than
                             # horizontal; accuracy_m reports only horizontal


def gps_sigma_m(accuracy_m, default=DEFAULT_GPS_SIGMA_M):
    """Per-fix 1-sigma for ekf.update_gps, from the packet accuracy field."""
    if accuracy_m is None or accuracy_m < 0:
        return default
    return max(MIN_GPS_SIGMA_M, min(MAX_GPS_SIGMA_M, float(accuracy_m)))


class AltitudeSanity:
    """Cheap standing check on the vendor-dependent altitude question.

    Watches the spread of reported altitudes and how far they sit from the
    session origin. It cannot tell ellipsoidal from orthometric on its own -
    only a known ground elevation can - but it does catch the two failures
    that show up in practice: an altitude that never changes (some devices
    report a constant when they have no vertical fix) and one that wanders
    implausibly far for the flight envelope.
    """

    def __init__(self, max_plausible_span_m=500.0):
        self.max_plausible_span_m = max_plausible_span_m
        self.lo = None
        self.hi = None
        self.n = 0

    def observe(self, alt_m):
        self.n += 1
        self.lo = alt_m if self.lo is None else min(self.lo, alt_m)
        self.hi = alt_m if self.hi is None else max(self.hi, alt_m)

    def warnings(self):
        out = []
        if self.n < 5:
            return out
        span = self.hi - self.lo
        if span == 0.0:
            out.append("GPS altitude is constant across %d fixes - the device is "
                       "probably not producing a vertical fix; Z is unusable" % self.n)
        elif span > self.max_plausible_span_m:
            out.append("GPS altitude spans %.0f m across %d fixes, beyond the "
                       "plausible flight envelope" % (span, self.n))
        return out
