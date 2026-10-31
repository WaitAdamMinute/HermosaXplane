"""Street lights along the OSM road network (and decorative lamps on the pier)."""
import math
import random

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

from .osm import parse_length

ROAD_WIDTH = {"motorway": 22, "trunk": 18, "primary": 16, "secondary": 13, "tertiary": 11,
              "residential": 9, "unclassified": 8, "living_street": 7, "service": 5}
SPACING = {"primary": 32, "trunk": 32, "secondary": 35, "tertiary": 38, "residential": 48,
           "unclassified": 50, "living_street": 50}
DECORATIVE = {"pedestrian": 16, "footway": 24}


def _road_lines(osm, area_poly):
    out = []
    for wid, tags, nodes in osm.highways():
        pts = [osm.nodes[n] for n in nodes if n in osm.nodes]
        if len(pts) < 2:
            continue
        ln = LineString(pts)
        if not ln.intersects(area_poly):
            continue
        out.append((tags, ln))
    return out


def roads_union(osm, area_poly):
    lines = [ln for tags, ln in _road_lines(osm, area_poly) if tags["highway"] in ROAD_WIDTH]
    return unary_union(lines) if lines else None


def place(osm, area_poly, building_polys, lib, frame, pier_polys, pier_cfg, seed=3):
    rnd = random.Random(seed)
    inside = prep(area_poly)
    bidx = STRtree(building_polys) if building_polys else None
    placed_pts = []
    pidx = {}
    out = []

    def blocked(p, clear):
        if bidx is not None:
            for i in bidx.query(p.buffer(clear)):
                if building_polys[i].distance(p) < clear:
                    return True
        gx, gy = int(p.x // 10), int(p.y // 10)
        for ix in range(gx - 1, gx + 2):
            for iy in range(gy - 1, gy + 2):
                for q in pidx.get((ix, iy), ()):
                    if q.distance(p) < 9.0:
                        return True
        return False

    def add(obj, p, heading):
        lon, lat = frame.to_ll(p.x, p.y)
        out.append((obj, lon, lat, heading % 360))
        pidx.setdefault((int(p.x // 10), int(p.y // 10)), []).append(p)
        placed_pts.append(p)

    lamps = osm.nodes_tagged("highway", "street_lamp")
    lines = _road_lines(osm, area_poly)
    if len(lamps) >= 50:
        # OSM has the real lamp inventory: use it, orient arms toward nearest road
        rl = [ln for t, ln in lines if t["highway"] in ROAD_WIDTH]
        ridx = STRtree(rl) if rl else None
        for (x, y), tags in lamps:
            p = Point(x, y)
            if not inside.contains(p):
                continue
            hd = 0.0
            if ridx is not None:
                ln = rl[ridx.nearest(p)]
                q = ln.interpolate(ln.project(p))
                hd = math.degrees(math.atan2(q.x - p.x, q.y - p.y)) - 90
            add(lib.street_light("cobra"), p, hd)
        print(f"  street lights: {len(out)} from OSM street_lamp nodes")
    else:
        for tags, ln in lines:
            hw = tags["highway"]
            if hw in SPACING:
                width = parse_length(tags.get("width"))
                if width is None and tags.get("lanes"):
                    try:
                        width = float(tags["lanes"]) * 3.4 + 2.5
                    except ValueError:
                        width = None
                width = width or ROAD_WIDTH[hw]
                off = width / 2 + 0.9
                sp = SPACING[hw]
                both = hw in ("primary", "trunk", "secondary")
                L = ln.length
                s = rnd.uniform(4, sp / 2)
                k = 0
                while s < L - 4:
                    sides = (-1, 1) if both else ((-1,) if k % 2 == 0 else (1,))
                    for side in sides:
                        ss = s + (sp / 2 if (both and side > 0) else 0)
                        if ss >= L - 4:
                            continue
                        a = ln.interpolate(ss)
                        b = ln.interpolate(min(L, ss + 1.0))
                        dx, dy = b.x - a.x, b.y - a.y
                        n = math.hypot(dx, dy) or 1.0
                        nx, ny = -dy / n * side, dx / n * side   # left normal * side
                        p = Point(a.x + nx * off, a.y + ny * off)
                        if not inside.contains(p) or blocked(p, 1.2):
                            continue
                        # arm (+X in object) must point back to road centre
                        hd = math.degrees(math.atan2(-nx, -ny)) - 90
                        add(lib.street_light("cobra"), p, hd)
                    s += sp
                    k += 1
            elif hw in DECORATIVE and tags.get("footway") not in ("sidewalk", "crossing"):
                L = ln.length
                s = rnd.uniform(2, DECORATIVE[hw] / 2)
                while s < L - 2:
                    a = ln.interpolate(s)
                    b = ln.interpolate(min(L, s + 1.0))
                    dx, dy = b.x - a.x, b.y - a.y
                    n = math.hypot(dx, dy) or 1.0
                    p = Point(a.x - dy / n * 2.5, a.y + dx / n * 2.5)
                    if inside.contains(p) and not blocked(p, 0.8):
                        add(lib.street_light("acorn"), p, 0)
                    s += DECORATIVE[hw]
        print(f"  street lights: {len(out)} generated along roads")

    # Pier: acorn lamps along both edges at deck height
    n0 = len(out)
    for poly in pier_polys:
        mrr = poly.minimum_rotated_rectangle
        c = list(mrr.exterior.coords)
        e = [(c[i], c[i + 1]) for i in range(4)]
        e.sort(key=lambda s: -math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]))
        for (x0, y0), (x1, y1) in e[:2]:
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 20:
                continue
            s = 6.0
            while s < L - 6:
                t = s / L
                p = Point(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
                q = poly.exterior.interpolate(poly.exterior.project(p))
                inward = Point(poly.centroid.x - q.x, poly.centroid.y - q.y)
                ln = math.hypot(inward.x, inward.y) or 1
                p = Point(q.x + inward.x / ln * 0.7, q.y + inward.y / ln * 0.7)
                if poly.contains(p):
                    add(lib.street_light("acorn", base=pier_cfg["deck_height"]), p, 0)
                s += 18.0
    if len(out) > n0:
        print(f"  pier lamps: {len(out) - n0}")
    return out
