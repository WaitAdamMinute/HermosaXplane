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
from shapely.strtree import STRtree
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


def _uvq(sw):
    return [(sw.u0, sw.v0), (sw.u1, sw.v0), (sw.u1, sw.v1), (sw.u0, sw.v1)]


def _wall_ring(mesh, ring, z_bot, z_top, style, bay_w, storeys, roof_only=False, edge_info=None,
               atlas=None):
    """Walls along one ring (local x east / y north metres).

    edge_info(i, x0, y0, x1, y1, nx, ny) -> dict(garage=bool, balcony=bool)
    """
    cs = list(ring.coords)
    fh = (z_top - 0.0) / storeys
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(cs[:-1], cs[1:])):
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        if L < 0.05:
            continue
        n = (dy / L, 0.0, dx / L)  # outward (right of CCW edge) in OBJ frame: (east, up, south)
        info = edge_info(i, x0, y0, x1, y1, dy / L, -dx / L) if edge_info else {}
        bays = max(1, round(L / bay_w))
        gbays = set()
        if info.get("garage") and "garage" in style and not roof_only:
            mid = bays // 2
            gbays = {mid, mid - 1} if bays >= 4 else {mid}
        for b in range(bays):
            ax, ay = x0 + dx * b / bays, y0 + dy * b / bays
            bx, by = x0 + dx * (b + 1) / bays, y0 + dy * (b + 1) / bays
            if roof_only:
                levels = [(z_top - 0.35, z_top, style["blank"])]
            else:
                levels = [(z_bot, 0.0, style["blank"])]
                for k in range(storeys):
                    if k == 0:
                        sw = style["garage"] if b in gbays else style["ground"]
                    else:
                        sw = style["upper"]
                    levels.append((k * fh, (k + 1) * fh, sw))
            for lo, hi, sw in levels:
                if hi - lo < 0.01:
                    continue
                # OBJ frame z = -north
                mesh.quad((ax, lo, -ay), (bx, lo, -by), (bx, hi, -by), (ax, hi, -ay), n, _uvq(sw))
            if info.get("balcony") and atlas is not None and not roof_only and storeys >= 2 and L >= 3.0:
                for k in range(1, storeys):
                    _balcony(mesh, ax, ay, bx, by, dy / L, -dx / L, k * fh, atlas)


def _balcony(m, ax, ay, bx, by, nx, ny, y, atlas, depth=1.1):
    """Slab + railing projecting from the wall segment a-b (outward normal nx, ny)."""
    ux, uy = bx - ax, by - ay
    inset = 0.15
    ax2, ay2 = ax + ux * inset, ay + uy * inset
    bx2, by2 = bx - ux * inset, by - uy * inset
    cx, cy = ax2 + nx * depth, ay2 + ny * depth
    dx_, dy_ = bx2 + nx * depth, by2 + ny * depth
    t = 0.2
    slab, rail = atlas.slab, atlas.railing
    up, dn = (0, 1, 0), (0, -1, 0)
    for yy, nrm in ((y, up), (y - t, dn)):
        m.quad((ax2, yy, -ay2), (bx2, yy, -by2), (dx_, yy, -dy_), (cx, yy, -cy), nrm, _uvq(slab))
    fn = (nx, 0.0, -ny)
    m.quad((cx, y - t, -cy), (dx_, y - t, -dy_), (dx_, y, -dy_), (cx, y, -cy), fn, _uvq(slab))
    h = 1.0
    m.quad((cx, y, -cy), (dx_, y, -dy_), (dx_, y + h, -dy_), (cx, y + h, -cy), fn, _uvq(rail))
    for (px, py), (qx, qy) in (((ax2, ay2), (cx, cy)), ((bx2, by2), (dx_, dy_))):
        m.quad((px, y, -py), (qx, y, -qy), (qx, y + h, -qy), (px, y + h, -py), (ux, 0, -uy), _uvq(rail))


def _roof_deck_railing(m, ring, z, atlas, inset=0.25, h=1.05):
    cs = list(ring.coords)
    for (x0, y0), (x1, y1) in zip(cs[:-1], cs[1:]):
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 1.0:
            continue
        nx, ny = (y1 - y0) / L, -(x1 - x0) / L
        segs = max(1, round(L / 2.5))
        for k in range(segs):
            ax = x0 + (x1 - x0) * k / segs - nx * inset
            ay = y0 + (y1 - y0) * k / segs - ny * inset
            bx = x0 + (x1 - x0) * (k + 1) / segs - nx * inset
            by = y0 + (y1 - y0) * (k + 1) / segs - ny * inset
            m.quad((ax, z, -ay), (bx, z, -by), (bx, z + h, -by), (ax, z + h, -ay), (nx, 0, -ny),
                   _uvq(atlas.cable_railing))


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


def _tri_normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    L = math.sqrt(sum(q * q for q in n)) or 1.0
    n = tuple(q / L for q in n)
    return n if n[1] >= 0 else tuple(-q for q in n)


def _emit_tris(m, verts, uvs, idx):
    for k in range(0, len(idx), 3):
        a, b, c = (verts[idx[k]], verts[idx[k + 1]], verts[idx[k + 2]])
        m.tris([a, b, c], [uvs[idx[k]], uvs[idx[k + 1]], uvs[idx[k + 2]]], _tri_normal(a, b, c), [0, 1, 2])


def _rect_axes(poly):
    mrr = poly.minimum_rotated_rectangle
    c = list(mrr.exterior.coords)[:4]
    e0 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e1 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    l0, l1 = math.hypot(*e0), math.hypot(*e1)
    if l0 >= l1:
        u, L, W = (e0[0] / l0, e0[1] / l0), l0, l1
    else:
        u, L, W = (e1[0] / l1, e1[1] / l1), l1, l0
    cx, cy = mrr.centroid.x, mrr.centroid.y
    return mrr, (cx, cy), u, L, W


def _gable(m, poly, eave, rise, roof_uv, style, overhang):
    """Rectangular gable roof over the footprint's minimum rotated rectangle."""
    _, (cx, cy), u, L, W = _rect_axes(poly)
    v = (-u[1], u[0])
    hl, hw = L / 2 + overhang, W / 2 + overhang
    drop = overhang * rise / (W / 2)
    ridge = eave + rise

    def P(a, b, z):
        x, y = cx + u[0] * a + v[0] * b, cy + u[1] * a + v[1] * b
        return (x, z, -y), roof_uv(x, y)

    for side in (-1, 1):
        q = [P(-hl, side * hw, eave - drop), P(hl, side * hw, eave - drop), P(hl, 0, ridge), P(-hl, 0, ridge)]
        verts, uvs = [a for a, _ in q], [b for _, b in q]
        _emit_tris(m, verts, uvs, [0, 1, 2, 0, 2, 3])
    # gable end triangles (wall material) at the true wall line
    sw = style["blank"]
    for end in (-1, 1):
        a = end * L / 2
        q = [P(a, -W / 2, eave)[0], P(a, W / 2, eave)[0], P(a, 0, ridge)[0]]
        n = (u[0] * end, 0.0, -u[1] * end)
        m.tris(q, [(sw.u0, sw.v0), (sw.u1, sw.v0), ((sw.u0 + sw.u1) / 2, sw.v1)], n, [0, 1, 2])


def _hip(m, poly, eave, pitch_t, roof_uv, overhang):
    """Hip roof for any footprint: sloped band from the eave line up to an
    inset ridge outline (approximates the straight skeleton)."""
    _, _, _, L, W = _rect_axes(poly)
    d = 0.48 * W
    inner = poly.buffer(-d, join_style=2)
    outer = poly.buffer(overhang, join_style=2)
    if outer.geom_type != "Polygon":
        return None
    oc = list(orient(outer, 1.0).exterior.coords)[:-1]
    z_out = eave - overhang * pitch_t
    if inner.is_empty or inner.geom_type != "Polygon" or inner.area < 0.05 or not outer.contains(inner):
        # pyramid / degenerate: everything rises to the centroid
        rise = 0.5 * W * pitch_t
        c = poly.centroid
        verts = [(x, z_out, -y) for x, y in oc] + [(c.x, eave + rise, -c.y)]
        uvs = [roof_uv(x, y) for x, y in oc] + [roof_uv(c.x, c.y)]
        n = len(oc)
        idx = []
        for k in range(n):
            idx += [k, (k + 1) % n, n]
        _emit_tris(m, verts, uvs, idx)
        return eave + rise
    ic = list(orient(inner, -1.0).exterior.coords)[:-1]
    ridge = eave + d * pitch_t
    pts = oc + ic
    idx = earcut.triangulate_float64(np.array(pts, dtype=np.float64),
                                     np.array([len(oc), len(pts)], dtype=np.uint32)).tolist()
    zs = [z_out] * len(oc) + [ridge] * len(ic)
    verts = [(x, z, -y) for (x, y), z in zip(pts, zs)]
    uvs = [roof_uv(x, y) for x, y in pts]
    _emit_tris(m, verts, uvs, idx)
    _cap(m, inner, ridge, roof_uv)
    return ridge


def build_mesh(poly_local, height, style, roof_uv, cfg, roof_only=False, roof=None, edge_info=None,
               atlas=None, deck=False):
    """poly_local: footprint in metres relative to the object origin.

    roof: dict(kind="flat"|"gable"|"hip", pitch=deg).  For pitched roofs the
    measured height is the ridge; walls stop at the eaves.
    """
    poly = orient(poly_local, sign=1.0)
    diam = math.sqrt(poly.area) * 1.4
    z_bot = -(cfg["basement"] + 0.12 * diam)
    kind = (roof or {}).get("kind", "flat") if not roof_only else "flat"
    m = Mesh()
    eave = height
    if kind in ("gable", "hip"):
        t = math.tan(math.radians(roof.get("pitch", 24)))
        _, _, _, L, W = _rect_axes(poly)
        rise = (W / 2) * t if kind == "gable" else 0.48 * W * t
        if height - rise < 2.6:
            rise = height - 2.6
            t = rise / (W / 2 if kind == "gable" else 0.48 * W)
        if rise < 0.8:
            kind = "flat"
        else:
            eave = height - rise
        if kind == "gable":
            mrr = poly.minimum_rotated_rectangle
            poly = orient(mrr, sign=1.0)
    storeys = max(1, round(eave / 3.0))
    for ri, ring in enumerate([poly.exterior] + list(poly.interiors)):
        _wall_ring(m, ring, z_bot, eave, style, cfg["bay_width"], storeys, roof_only,
                   edge_info if ri == 0 else None, atlas)
    overhang = cfg.get("eave_overhang", 0.45)
    if kind == "gable":
        _gable(m, poly, eave, height - eave, roof_uv, style, overhang)
    elif kind == "hip":
        if _hip(m, poly, eave, t, roof_uv, overhang) is None:
            _cap(m, poly, eave, roof_uv)
    else:
        _cap(m, poly, height, roof_uv)
        if roof_only:
            _cap(m, poly, height - 0.35, roof_uv, up=False)
        elif deck and atlas is not None:
            _roof_deck_railing(m, poly.exterior, height, atlas)
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


def make_objects(items, frame, grid, atlas, cfg, out_dir, rel_tex, rel_lit, kind="building", pier_cfg=None,
                 ctx=None):
    """Write one OBJ per footprint. Returns list of (obj_relpath, lon, lat).

    ctx (buildings only): dict(roof=callable(poly_world, tags, h) -> roof spec,
    alleys=STRtree-able list of LineStrings, coast=geometry)."""
    os.makedirs(out_dir, exist_ok=True)
    ctx = ctx or {}
    placed = []
    skipped = 0
    stats = {"gable": 0, "hip": 0, "flat": 0, "garage_walls": 0, "balcony_walls": 0, "decks": 0}
    alleys = ctx.get("alleys") or []
    alley_idx = STRtree(alleys) if alleys else None
    coast = ctx.get("coast")
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
            coast_d = coast.distance(c) if coast is not None else 1e9
            style = atlas.style_for(oid, tags, h, coast_d)
            residential = "garage" in style
            roof = ctx["roof"](poly, tags, h) if ctx.get("roof") else {"kind": "flat"}
            hsh = (sum(ord(ch) for ch in oid) * 2654435761) & 0xFFFF
            want_balc = residential and h >= 5.5 and coast_d < 700 and (hsh % 100) < 70
            flags = {"g": 0, "b": 0}

            def edge_info(i, x0, y0, x1, y1, nx, ny, cx=c.x, cy=c.y):
                out = {}
                mx, my = (x0 + x1) / 2 + cx, (y0 + y1) / 2 + cy
                if residential and alley_idx is not None:
                    pt = Point(mx + nx * 2.5, my + ny * 2.5)
                    for j in alley_idx.query(pt.buffer(4.0)):
                        if alleys[j].distance(pt) < 3.5:
                            out["garage"] = True
                            flags["g"] += 1
                            break
                if want_balc and nx < -0.55 and not out.get("garage"):
                    out["balcony"] = True
                    flags["b"] += 1
                return out

            deck = residential and roof["kind"] == "flat" and coast_d < 900 and h >= 6.0 and (hsh % 100) < 65
            mesh = build_mesh(local, h, style, roof_uv, cfg,
                              roof_only=tags.get("building") in ("roof", "carport"),
                              roof=roof, edge_info=edge_info, atlas=atlas, deck=deck)
            stats[roof["kind"]] = stats.get(roof["kind"], 0) + 1
            stats["garage_walls"] += flags["g"]
            stats["balcony_walls"] += flags["b"]
            stats["decks"] += int(deck)
        name = f"{oid}.obj"
        write_obj(os.path.join(out_dir, name), mesh,
                  f"{rel_tex}/{chunk.name}.{cfg['_tex_ext']}",
                  f"{rel_lit}" if rel_lit else None,
                  cfg["lod_far"])
        placed.append((name, lon, lat))
    print(f"  {kind}s: {len(placed)} objects written, {skipped} skipped")
    if kind != "pier":
        print("  roofs: {gable} gable, {hip} hip, {flat} flat; {garage_walls} alley garage walls, "
              "{balcony_walls} balcony walls, {decks} roof decks".format(**stats))
    return placed
