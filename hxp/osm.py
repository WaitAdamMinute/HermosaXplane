"""Download OpenStreetMap data (Overpass API) and turn it into shapely geometry.

Hermosa Beach buildings in OSM come from the 2017 LA County building import
(LARIAC), whose footprints were traced from 4-inch aerial imagery and carry
LiDAR-derived `height` tags, so they line up with the photo and have real
heights.
"""
import json
import re
import time
import urllib.parse
import urllib.request

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, polygonize, unary_union

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

USER_AGENT = "HermosaXplane/1.0 (X-Plane scenery builder)"


def overpass_query(area):
    s, w, n, e = area["south"], area["west"], area["north"], area["east"]
    bb = f"{s},{w},{n},{e}"
    # Coastline is fetched from a wider box so it can be closed against the
    # scenery boundary cleanly.
    cb = f"{s - 0.02},{w - 0.03},{n + 0.02},{e + 0.01}"
    return f"""
[out:json][timeout:300];
(
  way["building"]({bb});
  relation["building"]({bb});
  way["natural"="coastline"]({cb});
  way["man_made"="pier"]({bb});
  relation["man_made"="pier"]({bb});
  way["highway"]({bb});
  node["natural"="tree"]({bb});
  node["highway"="street_lamp"]({bb});
  way["natural"="tree_row"]({bb});
);
out body;
>;
out skel qt;
"""


def fetch(area, out_path, urls=None):
    q = overpass_query(area)
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in urls or OVERPASS_URLS:
        for attempt in range(3):
            try:
                print(f"  Overpass: {url} (attempt {attempt + 1})")
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=400) as r:
                    body = r.read()
                js = json.loads(body)
                if not js.get("elements"):
                    raise RuntimeError("empty Overpass response")
                with open(out_path, "wb") as f:
                    f.write(body)
                print(f"  saved {len(js['elements'])} OSM elements -> {out_path}")
                return
            except Exception as ex:  # noqa: BLE001 - try next mirror
                last = ex
                print(f"    failed: {ex}")
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


# --------------------------------------------------------------------------
# Parsing


def parse_length(v):
    """Parse an OSM length ("7.5", "25 ft", "7 m", "12'6\"") into metres."""
    if v is None:
        return None
    v = str(v).strip().lower().replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*'\s*(\d+(?:\.\d+)?)?\s*\"?$", v)
    if m:
        return float(m.group(1)) * 0.3048 + float(m.group(2) or 0) * 0.0254
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet|foot)?$", v)
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2) in ("ft", "feet", "foot"):
        x *= 0.3048
    return x


class OSMData:
    def __init__(self, path, frame):
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        self.frame = frame
        self.nodes = {}
        self.node_tags = {}
        self.ways = {}
        self.rels = []
        for el in js["elements"]:
            t = el["type"]
            if t == "node":
                self.nodes[el["id"]] = frame.to_xy(el["lon"], el["lat"])
                if el.get("tags"):
                    self.node_tags[el["id"]] = el["tags"]
            elif t == "way":
                if el["id"] in self.ways and "tags" not in el:
                    continue  # skeleton duplicate of a tagged way
                self.ways[el["id"]] = el
            elif t == "relation":
                self.rels.append(el)

    def way_coords(self, way):
        return [self.nodes[n] for n in way.get("nodes", []) if n in self.nodes]

    def way_polygon(self, way):
        c = self.way_coords(way)
        if len(c) < 4 or c[0] != c[-1]:
            return None
        p = Polygon(c)
        if not p.is_valid:
            p = p.buffer(0)
        return p if not p.is_empty else None

    def relation_polygon(self, rel):
        outers, inners = [], []
        for m in rel.get("members", []):
            if m["type"] != "way" or m["ref"] not in self.ways:
                continue
            c = self.way_coords(self.ways[m["ref"]])
            if len(c) >= 2:
                (inners if m.get("role") == "inner" else outers).append(LineString(c))

        def rings(lines):
            if not lines:
                return []
            return list(polygonize(linemerge(lines)))

        o = unary_union(rings(outers))
        if o.is_empty:
            return None
        i = unary_union(rings(inners))
        g = o.difference(i) if not i.is_empty else o
        return g if not g.is_empty else None

    @staticmethod
    def _parts(g):
        if g is None:
            return []
        if isinstance(g, Polygon):
            return [g]
        if isinstance(g, MultiPolygon):
            return list(g.geoms)
        return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]

    def tagged(self, key, value=None):
        """Yield (osm_id, tags, polygon) for closed ways / multipolygons."""
        for wid, w in self.ways.items():
            tags = w.get("tags", {})
            if key in tags and (value is None or tags[key] == value):
                for p in self._parts(self.way_polygon(w)):
                    yield f"w{wid}", tags, p
        for r in self.rels:
            tags = r.get("tags", {})
            if key in tags and (value is None or tags[key] == value):
                for p in self._parts(self.relation_polygon(r)):
                    yield f"r{r['id']}", tags, p

    def buildings(self):
        return [(i, t, p) for i, t, p in self.tagged("building") if t.get("building") != "no"]

    def piers(self, default_width):
        out = []
        for wid, w in self.ways.items():
            tags = w.get("tags", {})
            if tags.get("man_made") != "pier":
                continue
            c = self.way_coords(w)
            poly = self.way_polygon(w) if tags.get("area") != "no" else None
            if poly is None and len(c) >= 2:
                width = parse_length(tags.get("width")) or default_width
                poly = LineString(c).buffer(width / 2, cap_style=2, join_style=2)
            if poly is not None:
                out.append((f"w{wid}", tags, poly))
        for r in self.rels:
            if r.get("tags", {}).get("man_made") == "pier":
                for p in self._parts(self.relation_polygon(r)):
                    out.append((f"r{r['id']}", r["tags"], p))
        # merge overlapping pieces (pier deck + pier end platform etc.)
        if not out:
            return []
        merged = unary_union([p for _, _, p in out])
        return [(f"pier{i}", {}, p) for i, p in enumerate(self._parts(merged))]

    def coastlines(self):
        lines = []
        for w in self.ways.values():
            if w.get("tags", {}).get("natural") == "coastline":
                c = self.way_coords(w)
                if len(c) >= 2:
                    lines.append(LineString(c))
        if not lines:
            return []
        m = linemerge(lines)
        return list(m.geoms) if hasattr(m, "geoms") else [m]

    def nodes_tagged(self, key, value):
        return [(self.nodes[n], t) for n, t in self.node_tags.items() if t.get(key) == value]

    def highways(self):
        out = []
        for wid, w in self.ways.items():
            tags = w.get("tags", {})
            if "highway" in tags and tags.get("area") != "yes":
                out.append((wid, tags, w.get("nodes", [])))
        return out


def land_polygon(coastlines, bounds_poly):
    """Split the area polygon by the coastline; OSM keeps land on the left."""
    if not coastlines:
        return bounds_poly
    pieces = list(polygonize(unary_union([bounds_poly.exterior] + list(coastlines))))
    pieces = [p for p in pieces if p.intersects(bounds_poly) and p.area > 1.0]
    if len(pieces) <= 1:
        return bounds_poly
    land = []
    for p in pieces:
        votes = 0
        for line in coastlines:
            cs = list(line.coords)
            for (x0, y0), (x1, y1) in zip(cs[:-1], cs[1:]):
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                dx, dy = x1 - x0, y1 - y0
                ln = (dx * dx + dy * dy) ** 0.5
                if ln < 1e-6:
                    continue
                pl = Point(mx - dy / ln * 2.0, my + dx / ln * 2.0)  # 2 m to the left
                pr = Point(mx + dy / ln * 2.0, my - dx / ln * 2.0)
                if p.contains(pl):
                    votes += 1
                elif p.contains(pr):
                    votes -= 1
        if votes > 0:
            land.append(p)
    if not land:
        return bounds_poly
    return unary_union(land).intersection(bounds_poly)
