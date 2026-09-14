"""
The two priority outputs of a reconstruction - cloud_raw.ply and
mesh_poisson.ply - and the steps between the fused cloud and those files.

Both PLYs are written BINARY little-endian. The old ASCII writer formatted
every point through a Python string (slow at millions of points, and ~3x the
file size), and binary PLY is read by everything that reads PLY: MeshLab,
CloudCompare, Blender, three.js' PLYLoader, SuperSplat.
"""
import os

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree


def write_ply_points(path, pts, cols, normals=None, scalars=None):
    """pts (N,3), cols (N,3) uint8, normals (N,3) optional, scalars: optional
    dict name -> (N,) float32 per-vertex properties (e.g. view count)."""
    n = len(pts)
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if normals is not None:
        fields += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")]
    fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    for name in (scalars or {}):
        fields.append((name, "<f4"))
    arr = np.empty(n, dtype=fields)
    arr["x"], arr["y"], arr["z"] = pts[:, 0], pts[:, 1], pts[:, 2]
    if normals is not None:
        arr["nx"], arr["ny"], arr["nz"] = normals[:, 0], normals[:, 1], normals[:, 2]
    arr["red"], arr["green"], arr["blue"] = cols[:, 0], cols[:, 1], cols[:, 2]
    for name, v in (scalars or {}).items():
        arr[name] = v
    type_names = {"<f4": "float", "u1": "uchar", "<i4": "int"}
    header = ["ply", "format binary_little_endian 1.0", "element vertex %d" % n]
    header += ["property %s %s" % (type_names[t], name) for name, t in fields]
    header.append("end_header")
    with open(path, "wb") as f:
        f.write(("\n".join(header) + "\n").encode("ascii"))
        f.write(arr.tobytes())


def write_ply_mesh(path, verts, faces, cols=None, normals=None):
    nv, nf = len(verts), len(faces)
    vfields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if normals is not None:
        vfields += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")]
    if cols is not None:
        vfields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    v = np.empty(nv, dtype=vfields)
    v["x"], v["y"], v["z"] = verts[:, 0], verts[:, 1], verts[:, 2]
    if normals is not None:
        v["nx"], v["ny"], v["nz"] = normals[:, 0], normals[:, 1], normals[:, 2]
    if cols is not None:
        v["red"], v["green"], v["blue"] = cols[:, 0], cols[:, 1], cols[:, 2]
    f = np.empty(nf, dtype=[("n", "u1"), ("i", "<i4", (3,))])
    f["n"] = 3
    f["i"] = faces
    type_names = {"<f4": "float", "u1": "uchar"}
    header = ["ply", "format binary_little_endian 1.0", "element vertex %d" % nv]
    header += ["property %s %s" % (type_names[t], name) for name, t in vfields]
    header += ["element face %d" % nf, "property list uchar int vertex_indices", "end_header"]
    with open(path, "wb") as fh:
        fh.write(("\n".join(header) + "\n").encode("ascii"))
        fh.write(v.tobytes())
        fh.write(f.tobytes())


def statistical_outlier_mask(pts, k=12, std_ratio=2.0):
    """Keep-mask by the Open3D remove_statistical_outlier criterion: drop
    points whose mean distance to their k nearest neighbours is more than
    std_ratio standard deviations above the global mean. workers=-1 spreads
    the k-NN query over every CPU core (it dominates at millions of points)."""
    if len(pts) <= k + 1:
        return np.ones(len(pts), dtype=bool)
    tree = cKDTree(pts)
    d, _ = tree.query(pts, k=k + 1, workers=-1)
    md = d[:, 1:].mean(axis=1)
    return md < md.mean() + std_ratio * md.std()


def vertex_normals(verts, faces):
    fn = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]], verts[faces[:, 2]] - verts[faces[:, 0]])
    vn = np.zeros_like(verts)
    for j in range(3):
        np.add.at(vn, faces[:, j], fn)
    return (vn / np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-20)).astype(np.float32)


def poisson_mesh(pts, normals, cols, depth=10, trim_dist=None, min_component_faces=None,
                 threads=None, log=print):
    """Screened Poisson (pymeshlab / MeshLab's PoissonRecon) on an ORIENTED
    cloud, then two clean-ups Poisson always needs:

    1. Distance trim. Poisson fits a closed indicator function, so it
       invents surface wherever there is no data - the bubble around the
       scene and the lids over every hole. Faces with any vertex further
       than trim_dist from the nearest input point are removed; real surface
       is by construction near the points that produced it.
    2. Small-component removal: the trim leaves confetti where the invented
       surface grazed a few stray points.

    Colours come from the nearest input point (the same k-d tree query as
    the trim), so the mesh carries the cloud's photo colours.
    Returns (verts, faces, vcols, vnormals, info)."""
    import pymeshlab

    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=np.ascontiguousarray(pts, dtype=np.float64),
                               v_normals_matrix=np.ascontiguousarray(normals, dtype=np.float64)))
    ms.generate_surface_reconstruction_screened_poisson(
        depth=int(depth), samplespernode=1.5, pointweight=4.0, preclean=False,
        threads=int(threads or os.cpu_count() or 8))
    m = ms.current_mesh()
    V = m.vertex_matrix()
    Fc = m.face_matrix()
    info = {"poisson_vertices": len(V), "poisson_faces": len(Fc)}
    log("  poisson depth %d: %d vertices, %d faces" % (depth, len(V), len(Fc)))

    tree = cKDTree(pts)
    dist, nn = tree.query(V, k=1, workers=-1)
    if trim_dist is not None:
        keep_v = dist <= trim_dist
        keep_f = keep_v[Fc].all(axis=1)
        Fc = Fc[keep_f]
    # Connected components over the kept faces.
    nv = len(V)
    if len(Fc):
        rows = np.concatenate([Fc[:, 0], Fc[:, 1], Fc[:, 2]])
        colsg = np.concatenate([Fc[:, 1], Fc[:, 2], Fc[:, 0]])
        g = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, colsg)), shape=(nv, nv))
        _, labels = connected_components(g, directed=False)
        face_label = labels[Fc[:, 0]]
        sizes = np.bincount(face_label)
        if min_component_faces is None:
            min_component_faces = max(200, int(0.002 * len(Fc)))
        big = sizes >= min_component_faces
        Fc = Fc[big[face_label]]
        info["components_kept"] = int(big.sum())
        info["components_dropped"] = int((~big & (sizes > 0)).sum())
    used = np.zeros(nv, dtype=bool)
    used[Fc.reshape(-1)] = True
    remap = -np.ones(nv, dtype=np.int64)
    remap[used] = np.arange(used.sum())
    verts = V[used].astype(np.float32)
    faces = remap[Fc].astype(np.int32)
    vcols = cols[nn[used]]
    vn = vertex_normals(verts.astype(np.float64), faces) if len(faces) else np.zeros_like(verts)
    info.update({"vertices": len(verts), "faces": len(faces)})
    log("  after trim (%.4g) + component filter: %d vertices, %d faces"
        % (trim_dist if trim_dist is not None else float("nan"), len(verts), len(faces)))
    return verts, faces, vcols, vn, info
