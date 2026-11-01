"""Beach details: lifeguard towers and beach-volleyball nets at their real positions.

Sources: OpenStreetMap (volleyball pitches, lifeguard towers) and, when it
can be downloaded, LA County's public Lifeguard Towers layer.
"""
import json
import math
import os
import urllib.parse
import urllib.request

from shapely.geometry import Point

LACOUNTY_TOWERS = ("https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/"
                   "LMS_Data_Public/MapServer/140/query")


def fetch_lacounty_towers(area, out_path):
    q = urllib.parse.urlencode({
        "f": "json", "where": "1=1", "outFields": "*", "outSR": 4326, "returnGeometry": "true",
        "geometry": f"{area['west'] - 0.01},{area['south'] - 0.01},{area['east']},{area['north'] + 0.01}",
        "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
    })
    req = urllib.request.Request(f"{LACOUNTY_TOWERS}?{q}", headers={"User-Agent": "HermosaXplane/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        js = json.loads(r.read())
    if "error" in js:
        raise RuntimeError(js["error"])
    pts = [[f["geometry"]["x"], f["geometry"]["y"]] for f in js.get("features", []) if f.get("geometry")]
    with open(out_path, "w") as f:
        json.dump({"source": "LA County Lifeguard Towers", "towers": pts}, f)
    print(f"  {len(pts)} LA County lifeguard towers -> {out_path}")


def _is_tower(t):
    return (t.get("tower:type") == "lifeguard" or "lifeguard" in t.get("emergency", "")
            or t.get("lifeguard") in ("yes", "tower") or t.get("amenity") == "lifeguard"
            or (t.get("man_made") == "tower" and "lifeguard" in (t.get("name", "") + t.get("description", "")).lower()))


def collect(osm, frame, area_poly, county_json):
    towers, courts = [], []
    for nid, t in osm.node_tags.items():
        if nid not in osm.nodes:
            continue
        x, y = osm.nodes[nid]
        if not area_poly.contains(Point(x, y)):
            continue
        if _is_tower(t):
            towers.append((x, y))
        elif "volleyball" in t.get("sport", ""):
            courts.append(("point", Point(x, y)))
    for _, t, p in osm.tagged("sport"):
        if "volleyball" in t.get("sport", "") and area_poly.intersects(p):
            courts.append(("poly", p))
    for _, t, p in list(osm.tagged("emergency")) + list(osm.tagged("tower:type")):
        if _is_tower(t) and area_poly.intersects(p):
            c = p.centroid
            towers.append((c.x, c.y))
    if os.path.exists(county_json):
        with open(county_json) as f:
            for lon, lat in json.load(f)["towers"]:
                x, y = frame.to_xy(lon, lat)
                if area_poly.contains(Point(x, y)) and all(math.hypot(x - a, y - b) > 15 for a, b in towers):
                    towers.append((x, y))
    print(f"  beach: {len(towers)} lifeguard towers, {len(courts)} volleyball courts")
    return {"towers": towers, "courts": courts}


def drop_tower_footprints(items, feats):
    """Remove tiny OSM building footprints that are really the towers we model."""
    if not feats["towers"]:
        return items
    out = []
    for i, t, p in items:
        if p.area < 30 and any(Point(x, y).distance(p) < 6 for x, y in feats["towers"]):
            continue
        out.append((i, t, p))
    return out


def _bearing_to_sea(x, y, coast):
    if coast is None:
        return 270.0
    p = Point(x, y)
    q = coast.interpolate(coast.project(p)) if coast.geom_type == "LineString" else None
    if q is None:
        best = min(coast.geoms, key=lambda g: g.distance(p))
        q = best.interpolate(best.project(p))
    return math.degrees(math.atan2(q.x - x, q.y - y)) % 360


def place(feats, lib, frame, coast):
    out = []
    for x, y in feats["towers"]:
        hd = _bearing_to_sea(x, y, coast) - 90          # model front (+X) faces the water
        lon, lat = frame.to_ll(x, y)
        out.append((lib.lifeguard_tower(), lon, lat, hd))
    for kind, g in feats["courts"]:
        if kind == "poly":
            mrr = g.minimum_rotated_rectangle
            c = list(mrr.exterior.coords)[:3]
            e0 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
            e1 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
            short = e0 if math.hypot(*e0) < math.hypot(*e1) else e1
            width = math.hypot(*short)
            hd = math.degrees(math.atan2(short[0], short[1])) - 90  # net runs across the short axis
            ctr = mrr.centroid
            x, y = ctr.x, ctr.y
            width = max(7.0, min(width + 1.0, 11.0))
        else:
            x, y = g.x, g.y
            hd = _bearing_to_sea(x, y, coast) - 90          # net runs toward the water
            width = 9.0
        lon, lat = frame.to_ll(x, y)
        out.append((lib.volleyball_net(width), lon, lat, hd))
    return out
