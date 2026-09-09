"""
Turns the dense point cloud (dense_stereo.py) into a textured mesh -
without Open3D (no Windows wheel for this machine's Python 3.13).

Approach: rasterize the cloud into a 2.5D DSM height grid (max Z per XY
cell - appropriate for an aerial single-pass survey, where the dominant
missing information is true vertical building facades anyway; this is
also literally how most drone-mapping software's default terrain/DSM
mesh works). Each fully-observed 2x2 group of grid points becomes two
triangles; cells with no points are left as holes rather than filled by
interpolation, so the mesh never shows geometry nobody actually saw.
The same height/color grid doubles as the DSM/orthomosaic raster
(export.py's PNG + world-file output).
"""
import numpy as np
import cv2
import os


def build_grid(points, colors, cell_size_m, bounds=None):
    if bounds is None:
        min_xy = points[:, :2].min(axis=0)
        max_xy = points[:, :2].max(axis=0)
    else:
        min_xy, max_xy = bounds

    nx = int(np.ceil((max_xy[0] - min_xy[0]) / cell_size_m)) + 1
    ny = int(np.ceil((max_xy[1] - min_xy[1]) / cell_size_m)) + 1
    nx, ny = max(nx, 2), max(ny, 2)

    ix = np.clip(((points[:, 0] - min_xy[0]) / cell_size_m).astype(np.int64), 0, nx - 1)
    iy = np.clip(((points[:, 1] - min_xy[1]) / cell_size_m).astype(np.int64), 0, ny - 1)
    flat_idx = iy * nx + ix

    height_sum = np.zeros(nx * ny)
    height_max = np.full(nx * ny, -np.inf)
    color_sum = np.zeros((nx * ny, 3))
    count = np.zeros(nx * ny)

    np.add.at(height_sum, flat_idx, points[:, 2])
    np.maximum.at(height_max, flat_idx, points[:, 2])
    np.add.at(color_sum, flat_idx, colors)
    np.add.at(count, flat_idx, 1)

    observed = count > 0
    height_grid = np.where(observed, height_max, 0.0).reshape(ny, nx)
    color_grid = np.zeros((ny, nx, 3))
    color_grid[observed.reshape(ny, nx)] = (color_sum[observed] / count[observed, None])
    observed_grid = observed.reshape(ny, nx)

    return {
        "height": height_grid, "color": color_grid, "observed": observed_grid,
        "min_xy": min_xy, "cell_size_m": cell_size_m, "nx": nx, "ny": ny,
    }


def write_textured_mesh(grid, obj_path, texture_size=1024):
    height = grid["height"]
    color = grid["color"]
    observed = grid["observed"]
    min_xy = grid["min_xy"]
    cell = grid["cell_size_m"]
    rows, cols = height.shape

    verts = []
    vert_idx = -np.ones((rows, cols), dtype=np.int64)
    for j in range(rows):
        for i in range(cols):
            x = min_xy[0] + i * cell
            y = min_xy[1] + j * cell
            z = height[j, i]
            vert_idx[j, i] = len(verts)
            verts.append((x, y, z))

    faces = []
    n_covered_cells, n_total_cells = 0, 0
    for j in range(rows - 1):
        for i in range(cols - 1):
            n_total_cells += 1
            quad_ok = observed[j, i] and observed[j, i + 1] and observed[j + 1, i] and observed[j + 1, i + 1]
            if not quad_ok:
                continue
            n_covered_cells += 1
            a, b, c, d = vert_idx[j, i], vert_idx[j, i + 1], vert_idx[j + 1, i], vert_idx[j + 1, i + 1]
            faces.append((a, b, d))
            faces.append((a, d, c))

    mtl_path = os.path.splitext(obj_path)[0] + ".mtl"
    tex_path = os.path.splitext(obj_path)[0] + "_texture.png"
    mtl_name = os.path.basename(mtl_path)
    tex_name = os.path.basename(tex_path)

    tex_img = cv2.resize(
        cv2.cvtColor(np.nan_to_num(color).astype(np.uint8), cv2.COLOR_RGB2BGR),
        (texture_size, texture_size), interpolation=cv2.INTER_LINEAR,
    )
    cv2.imwrite(tex_path, tex_img)

    with open(mtl_path, "w") as f:
        f.write(f"newmtl mesh_material\nmap_Kd {tex_name}\n")

    with open(obj_path, "w") as f:
        f.write(f"mtllib {mtl_name}\nusemtl mesh_material\n")
        for x, y, z in verts:
            f.write(f"v {x} {y} {z}\n")
        for j in range(rows):
            v = 1.0 - j / max(1, rows - 1)
            for i in range(cols):
                u = i / max(1, cols - 1)
                f.write(f"vt {u} {v}\n")
        for a, b, c in faces:
            f.write(f"f {a+1}/{a+1} {b+1}/{b+1} {c+1}/{c+1}\n")

    completeness = n_covered_cells / max(1, n_total_cells)
    return {
        "obj_path": obj_path, "texture_path": tex_path,
        "n_vertices": len(verts), "n_faces": len(faces),
        "completeness": completeness,
    }
