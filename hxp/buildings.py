"""3D building and pier generation as X-Plane OBJ8 files.

Every building is extruded from its real (imagery-traced) footprint to its
real height.  The roof is textured with the aerial photo itself (so roofs
look exactly as they do from the air), the walls with the generated atlas.
Each building is its own object, placed at its footprint centroid, so it
sits on X-Plane's terrain at the right elevation even on hillsides.
"""
import math
import os

import mapbox_earcut as earcut
import numpy as np
from shapely.geometry import Point, Polygon
from shapely.geometry.polygon import orient

from .osm import parse_length


class Mesh:
    def __init__(self):
        self.v = []   # (x, y, z, nx, ny, nz, s, t)  OBJ frame: x east, y up, z south
        self.i = []

    def quad(self, p0, p1, p2, p3, n, uv):
        """p0..p3 counter-clockwise seen from outside; uv list of 4 (s, t)."""
        b = len(self.v)
        for p, (s, t) in zip((p0, p1, p2, p3), uv):
            self.v.append((p[0], p[1], p[2], n[0], n[1], n[2], s, t))
        self.i += [b, b + 1, b + 2, b, b + 2, b + 3]

    def tris(self, pts, uvs, n, idx):
        b = len(self.v)
        for p, (s, t) in zip(pts, uvs):
            self.v.append((p[0], p[1], p[2], n[0], n[1], n[2], s, t))
        self.i += [b + k for k in idx]


def write_obj(path, mesh, texture, texture_lit, lod_far, extra_attrs=()):
    nv, ni = len(mesh.v), len(mesh.i)
    lines = ["I", "800", "OBJ", "", f"TEXTURE {texture}"]
    if texture_lit:
        lines.append(f"TEXTURE_LIT {texture_lit}")
    lines += [f"POINT_COUNTS {nv} 0 0 {ni}", ""]
    for x, y, z, a, b, c, s, t in mesh.v:
        lines.append(f"VT {x:.3f} {y:.3f} {z:.3f} {a:.3f} {b:.3f} {c:.3f} {s:.6f} {t:.6f}")
    lines.append("")
    k = 0
    while ni - k >= 10:
        lines.append("IDX10 " + " ".join(str(j) for j in mesh.i[k:k + 10]))
        k += 10
    while k < ni:
        lines.append(f"IDX {mesh.i[k]}")
        k += 1
    lines += ["", f"ATTR_LOD 0 {lod_far:.0f}", "ATTR_no_cull", "ATTR_no_blend 0.5"]
    lines += list(extra_attrs)
    lines += [f"TRIS 0 {ni}", ""]
    with open(path, "w", newline="\n") as f:
        f.write("\n".join(lines))


# --------------------------------------------------------------------------


def building_height(tags, cfg):
    h = parse_length(tags.get("height"))
    if h is None:
        lv = parse_length(tags.get("building:levels"))
        if lv is not None:
            h = lv * cfg["level_height"] + 1.0
            rh = parse_length(tags.get("roof:height"))
            if rh:
                h += rh
    if h is None:
        b = tags.get("building")
        h = {"garage": 3.0, "garages": 3.0, "shed": 2.6, "carport": 2.8, "roof": 3.2,
             "apartments": 10.0, "commercial": 7.5, "retail": 6.5, "church": 11.0,
             "school": 8.0, "hotel": 12.0}.get(b, cfg["default_height"])
    return max(2.2, min(h, 200.0))


def _wall_ring(mesh, ring, z_bot, z_top, style, bay_w, storeys, roof_only=False):
    """Walls along one ring (local x east / y north metres)."""
    cs = list(ring.coords)
    fh = (z_top - 0.0) / storeys
    for (x0, y0), (x1, y1) in zip(cs[:-1], cs[1:]):
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        if L < 0.05:
            continue
        n = (dy / L, 0.0, dx / L)  # outward (right of CCW edge) in OBJ frame: (east, up, south)
        bays = max(1, round(L / bay_w))
        for b in range(bays):
            ax, ay = x0 + dx * b / bays, y0 + dy * b / bays
            bx, by = x0 + dx * (b + 1) / bays, y0 + dy * (b + 1) / bays
            if roof_only:
                levels = [(z_top - 0.35, z_top, style["blank"])]
            else:
                levels = [(z_bot, 0.0, style["blank"])]
                for k in range(storeys):
                    levels.append((k * fh, (k + 1) * fh, style["ground"] if k == 0 else style["upper"]))
            for lo, hi, sw in levels:
                if hi - lo < 0.01:
                    continue
                uv = [(sw.u0, sw.v0), (sw.u1, sw.v0), (sw.u1, sw.v1), (sw.u0, sw.v1)]
                # OBJ frame z = -north
                mesh.quad((ax, lo, -ay), (bx, lo, -by), (bx, hi, -by), (ax, hi, -ay), n, uv)


def _cap(mesh, poly, z, uv_fn, up=True):
    rings = [poly.exterior] + list(poly.interiors)
    pts, ends = [], []
    for r in rings:
        pts += list(r.coords)[:-1]
        ends.append(len(pts))
    arr = np.array(pts, dtype=np.float64)
    idx = earcut.triangulate_float64(arr, np.array(ends, dtype=np.uint32)).tolist()
    if not idx:
        return
    verts = [(x, z, -y) for x, y in pts]
    uvs = [uv_fn(x, y) for x, y in pts]
    mesh.tris(verts, uvs, (0.0, 1.0 if up else -1.0, 0.0), idx)


def build_mesh(poly_local, height, style, roof_uv, cfg, roof_only=False):
    """poly_local: footprint in metres relative to the object origin."""
    poly = orient(poly_local, sign=1.0)
    diam = math.sqrt(poly.area) * 1.4
    z_bot = -(cfg["basement"] + 0.12 * diam)
    storeys = max(1, round(height / 3.0))
    m = Mesh()
    for ring in [poly.exterior] + list(poly.interiors):
        _wall_ring(m, ring, z_bot, height, style, cfg["bay_width"], storeys, roof_only)
    _cap(m, poly, height, roof_uv)
    if roof_only:
        _cap(m, poly, height - 0.35, roof_uv, up=False)
    return m


def pier_mesh(poly_local, style, deck_uv, cfg):
    """Deck slab (photo-textured top) with concrete edges and pilings."""
    poly = orient(poly_local, sign=1.0)
    top = cfg["deck_height"]
    bot = top - cfg["deck_thickness"]
    m = Mesh()
    for ring in [poly.exterior] + list(poly.interiors):
        cs = list(ring.coords)
        for (x0, y0), (x1, y1) in zip(cs[:-1], cs[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 0.05:
                continue
            n = ((y1 - y0) / L, 0.0, (x1 - x0) / L)
            sw = style["ground"]
            segs = max(1, round(L / 4.0))
            for k in range(segs):
                ax, ay = x0 + (x1 - x0) * k / segs, y0 + (y1 - y0) * k / segs
                bx, by = x0 + (x1 - x0) * (k + 1) / segs, y0 + (y1 - y0) * (k + 1) / segs
                uv = [(sw.u0, sw.v0), (sw.u1, sw.v0), (sw.u1, sw.v1), (sw.u0, sw.v1)]
                m.quad((ax, bot, -ay), (bx, bot, -by), (bx, top, -by), (ax, top, -ay), n, uv)
    _cap(m, poly, top, deck_uv)
    _cap(m, poly, bot, lambda x, y: (style["blank"].u0, style["blank"].v0), up=False)
    # Pilings on a grid aligned with the pier's long axis.
    mrr = poly.minimum_rotated_rectangle
    c = list(mrr.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    ang = math.atan2(e1[1], e1[0])
    ca, sa = math.cos(ang), math.sin(ang)
    inner = poly.buffer(-1.0)
    if not inner.is_empty:
        sp = cfg["piling_spacing"]
        minx, miny, maxx, maxy = poly.bounds
        R = math.hypot(maxx - minx, maxy - miny)
        cx, cy = poly.centroid.x, poly.centroid.y
        sw = style["ground"]
        uv = [(sw.u0, sw.v0), (sw.u1, sw.v0), (sw.u1, sw.v1), (sw.u0, sw.v1)]
        r = 0.35
        a = -R
        while a <= R:
            b = -R
            while b <= R:
                px, py = cx + a * ca - b * sa, cy + a * sa + b * ca
                if inner.contains(Point(px, py)):
                    corners = [(px - r, py - r), (px + r, py - r), (px + r, py + r), (px - r, py + r), (px - r, py - r)]
                    for (x0, y0), (x1, y1) in zip(corners[:-1], corners[1:]):
                        L = math.hypot(x1 - x0, y1 - y0)
                        n = ((y1 - y0) / L, 0.0, (x1 - x0) / L)
                        m.quad((x0, -4.0, -y0), (x1, -4.0, -y1), (x1, bot, -y1), (x0, bot, -y0), n, uv)
                b += sp / 2
            a += sp
    return m


def make_objects(items, frame, grid, atlas, cfg, out_dir, rel_tex, rel_lit, kind="building", pier_cfg=None):
    """Write one OBJ per footprint. Returns list of (obj_relpath, lon, lat)."""
    os.makedirs(out_dir, exist_ok=True)
    placed = []
    skipped = 0
    for oid, tags, poly in items:
        if poly.area < cfg["min_area"]:
            skipped += 1
            continue
        c = poly.representative_point() if kind == "pier" else poly.centroid
        lon, lat = frame.to_ll(c.x, c.y)
        chunk = grid.chunk_for(lon, lat)
        if chunk is None:
            skipped += 1
            continue
        local = Polygon(
            [(x - c.x, y - c.y) for x, y in poly.exterior.coords],
            [[(x - c.x, y - c.y) for x, y in r.coords] for r in poly.interiors],
        ).simplify(0.05)
        if local.is_empty or not local.is_valid or local.geom_type != "Polygon":
            local = local.buffer(0)
            if local.geom_type != "Polygon":
                skipped += 1
                continue

        def roof_uv(x, y, cx=c.x, cy=c.y, ch=chunk):
            return ch.uv_clamped(*frame.to_ll(x + cx, y + cy))

        if kind == "pier":
            mesh = pier_mesh(local, atlas.styles["concrete"], roof_uv, pier_cfg)
        else:
            h = building_height(tags, cfg)
            style = atlas.style_for(oid, tags, h)
            mesh = build_mesh(local, h, style, roof_uv, cfg, roof_only=tags.get("building") in ("roof", "carport"))
        name = f"{oid}.obj"
        write_obj(os.path.join(out_dir, name), mesh,
                  f"{rel_tex}/{chunk.name}.{cfg['_tex_ext']}",
                  f"{rel_lit}" if rel_lit else None,
                  cfg["lod_far"])
        placed.append((name, lon, lat))
    print(f"  {kind}s: {len(placed)} objects written, {skipped} skipped")
    return placed
