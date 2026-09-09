"""
Standard-format exports (PS requirement D5-equivalent: "opens in QGIS/
CloudCompare, no custom loader").

- LAS via laspy (pure-Python wheel, confirmed installable on this
  machine's Python 3.13 - unlike pyproj/rasterio, see the plan doc).
  laspy's header offset/scale mechanism is designed exactly for
  large-magnitude UTM coordinates, so the point cloud is exported at its
  true georeferenced coordinates.
- Mesh OBJ/MTL/PNG: meshing.py already writes these directly; this module
  just records the local-ENU-to-UTM origin offset alongside them, since
  full UTM coordinates lose float precision in most OBJ viewers (a
  documented offset is the standard fix - see rtvio_3/PROJECT_CONTEXT.md
  section 7's "OBJ/GLB with a documented coordinate offset" note).
- DSM/orthomosaic as PNG + a world file (.pgw) + a .prj (WKT for the
  EPSG code): the dependency-free way to get something QGIS opens as a
  georeferenced raster without a true GeoTIFF writer (no rasterio wheel
  for this machine - see the plan doc).
"""
import json
import os
import numpy as np
import cv2
import laspy


def export_las(points_local_enu, colors, ref_lat_deg, ref_lon_deg, ref_alt_m, path):
    from .georeference import enu_to_utm_array, utm_zone_number

    utm_pts, epsg = enu_to_utm_array(points_local_enu, ref_lat_deg, ref_lon_deg, ref_alt_m)

    header = laspy.LasHeader(point_format=3, version="1.2")
    header.offsets = utm_pts.min(axis=0)
    header.scales = np.array([0.001, 0.001, 0.001])
    # laspy.LasHeader.add_crs() needs a live pyproj.CRS object internally
    # (imports pyproj even to build the old-style GeoTIFF VLR this
    # point-format/version combo would use) - not installed here (no cp313
    # Windows wheel, see the plan doc). Writing a WKT .prj sidecar instead:
    # not part of the LAS spec, but it's the same convention shapefiles use
    # and QGIS recognizes it next to a LAS/LAZ file too.
    las = laspy.LasData(header)
    las.x, las.y, las.z = utm_pts[:, 0], utm_pts[:, 1], utm_pts[:, 2]
    c = np.clip(colors, 0, 255).astype(np.uint16) * 256  # LAS wants 16-bit color
    las.red, las.green, las.blue = c[:, 0], c[:, 1], c[:, 2]
    las.write(path)

    if epsg is not None:
        zone = utm_zone_number(ref_lon_deg)
        lon0 = (zone - 1) * 6 - 180 + 3
        hemi = "N" if ref_lat_deg >= 0 else "S"
        fn = 0 if ref_lat_deg >= 0 else 10_000_000
        wkt = WKT_UTM_TEMPLATE.format(zone=zone, hemi=hemi, lon0=lon0, fn=fn, epsg=epsg)
        with open(os.path.splitext(path)[0] + ".prj", "w") as f:
            f.write(wkt)
    return epsg


def write_mesh_origin_sidecar(ref_lat_deg, ref_lon_deg, ref_alt_m, epsg, path):
    """The OBJ mesh keeps small local-ENU coordinates for float precision;
    this file records what UTM coordinate its (0,0,0) corresponds to."""
    from .georeference import enu_to_utm
    x0, y0, z0, _ = enu_to_utm(0, 0, 0, ref_lat_deg, ref_lon_deg, ref_alt_m)
    with open(path, "w") as f:
        json.dump({"origin_X_utm": x0, "origin_Y_utm": y0, "origin_Z": z0, "epsg": epsg}, f, indent=2)


WKT_UTM_TEMPLATE = (
    'PROJCS["WGS 84 / UTM zone {zone}{hemi}",'
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",{lon0}],'
    'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
    'PARAMETER["false_northing",{fn}],UNIT["metre",1],AUTHORITY["EPSG","{epsg}"]]'
)


def export_dsm_raster(grid, ref_lat_deg, ref_lon_deg, ref_alt_m, path_png):
    from .georeference import enu_to_utm, utm_zone_number

    height = grid["height"]
    observed = grid["observed"]
    cell = grid["cell_size_m"]
    min_xy = grid["min_xy"]

    valid = height[observed]
    lo, hi = (valid.min(), valid.max()) if len(valid) else (0, 1)
    norm = np.zeros_like(height)
    if hi > lo:
        norm = np.clip((height - lo) / (hi - lo), 0, 1)
    img = (norm * 255).astype(np.uint8)
    img[~observed] = 0
    # The grid is indexed [row = northing bin], so row 0 is its SOUTH edge,
    # while a north-up raster (which is what the world file below declares,
    # by putting the NW corner at the upper-left pixel with a negative
    # y-pixel-size) needs row 0 to be the NORTH edge. Without this flip the
    # DSM loads into QGIS mirrored about the east-west axis, sitting on top
    # of correctly-placed data from the same pipeline.
    cv2.imwrite(path_png, np.flipud(img))

    x0_utm, y0_utm, _, epsg = enu_to_utm(min_xy[0], min_xy[1] + height.shape[0] * cell, 0,
                                          ref_lat_deg, ref_lon_deg, ref_alt_m)
    pgw_path = os.path.splitext(path_png)[0] + ".pgw"
    with open(pgw_path, "w") as f:
        f.write(f"{cell}\n0.0\n0.0\n{-cell}\n{x0_utm}\n{y0_utm}\n")

    zone = utm_zone_number(ref_lon_deg)
    lon0 = (zone - 1) * 6 - 180 + 3
    hemi = "N" if ref_lat_deg >= 0 else "S"
    fn = 0 if ref_lat_deg >= 0 else 10_000_000
    wkt = WKT_UTM_TEMPLATE.format(zone=zone, hemi=hemi, lon0=lon0, fn=fn, epsg=epsg)
    prj_path = os.path.splitext(path_png)[0] + ".prj"
    with open(prj_path, "w") as f:
        f.write(wkt)

    return {"png": path_png, "pgw": pgw_path, "prj": prj_path, "min_height": lo, "max_height": hi, "epsg": epsg}
