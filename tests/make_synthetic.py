"""Create a fake data/ set (OSM + imagery) so the pipeline can be tested offline.

    python tests/make_synthetic.py /tmp/hxp-test
    python -m hxp build --root /tmp/hxp-test
"""
import json
import os
import random
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PIL import Image, ImageDraw  # noqa: E402

from hxp.geo import merc_px, px_lonlat  # noqa: E402
from hxp.imagery import Grid  # noqa: E402


def main(out):
    cfg = tomllib.load(open(os.path.join(os.path.dirname(__file__), "..", "config.toml"), "rb"))
    a = cfg["area"]
    rnd = random.Random(1)
    els, nid, wid = [], [1], [1]

    def node(lon, lat):
        nid[0] += 1
        els.append({"type": "node", "id": nid[0], "lon": lon, "lat": lat})
        return nid[0]

    def way(coords, tags, closed=True):
        ids = [node(*c) for c in coords]
        if closed:
            ids.append(ids[0])
        wid[0] += 1
        els.append({"type": "way", "id": wid[0], "nodes": ids, "tags": tags})

    coast_lon = lambda lat: -118.4015 + (lat - 33.85) * 0.12  # noqa: E731
    lats = [a["south"] - 0.02 + k * 0.002 for k in range(40)]
    way([(coast_lon(la), la) for la in reversed(lats)], {"natural": "coastline"}, closed=False)  # land on left (east) going south
    # pier
    pl = 33.8618
    way([(coast_lon(pl) + 0.0002, pl), (coast_lon(pl) - 0.0058, pl)], {"man_made": "pier", "width": "8"}, closed=False)
    # buildings: blocks east of the coast
    dlat, dlon = 9 / 111320, 9 / 92500
    for gy in range(0, 120, 4):
        lat0 = a["south"] + 0.001 + gy * 0.00022
        for gx in range(0, 60):
            lon0 = coast_lon(lat0) + 0.0012 + gx * 0.00035
            if lon0 > a["east"] - 0.001 or rnd.random() < 0.15:
                continue
            w, h = dlon * rnd.uniform(0.8, 1.4), dlat * rnd.uniform(1.2, 2.2)
            tags = {"building": rnd.choice(["house", "house", "apartments", "commercial", "garage"])}
            if rnd.random() < 0.8:
                tags["height"] = f"{rnd.uniform(4, 14):.1f}"
            coords = [(lon0, lat0), (lon0 + w, lat0), (lon0 + w, lat0 + h), (lon0, lat0 + h)]
            if rnd.random() < 0.2:  # L-shape
                coords = [(lon0, lat0), (lon0 + w, lat0), (lon0 + w, lat0 + h / 2), (lon0 + w / 2, lat0 + h / 2),
                          (lon0 + w / 2, lat0 + h), (lon0, lat0 + h)]
            way(coords, tags)
    # roads: a coast-parallel avenue and some cross streets, plus a few OSM trees
    way([(coast_lon(33.86) + 0.0008, 33.85 + k * 0.001) for k in range(26)], {"highway": "primary"}, closed=False)
    for k in range(8):
        la = 33.852 + k * 0.003
        way([(coast_lon(la) + 0.0004, la), (a["east"] - 0.001, la)], {"highway": "residential"}, closed=False)
    way([(coast_lon(33.8618) + 0.0001, 33.8622), (coast_lon(33.8618) + 0.0001, 33.8650)], {"highway": "pedestrian"}, closed=False)
    for k in range(20):
        nid[0] += 1
        els.append({"type": "node", "id": nid[0], "lon": a["west"] + 0.02 + k * 0.0005, "lat": 33.865,
                    "tags": {"natural": "tree", "genus": "Washingtonia" if k % 2 else "Quercus"}})

    os.makedirs(os.path.join(out, "data", "imagery"), exist_ok=True)
    json.dump({"elements": els}, open(os.path.join(out, "data", "osm.json"), "w"))
    grid = Grid(a, cfg["imagery"])
    for c in grid.chunks:
        im = Image.new("RGB", (grid.chunk_w, grid.chunk_h), (170, 165, 150))
        d = ImageDraw.Draw(im)
        for py in range(0, grid.chunk_h, 8):
            _, lat = px_lonlat(c.px0, c.py0 + py, grid.zoom)
            x, _ = merc_px(coast_lon(lat), lat, grid.zoom)
            if x > c.px0:
                d.rectangle([0, py, x - c.px0, py + 8], fill=(30, 80, 110))
        for k in range(0, grid.chunk_w, 256):
            d.line([(k, 0), (k, grid.chunk_h)], fill=(90, 90, 90), width=6)
        # lawns (smooth bright green) and tree crowns (dark, textured)
        trnd = random.Random(c.i * 7 + c.j)
        s_zoom = 2 ** (grid.zoom - 18)
        for _ in range(25):
            x, y = trnd.uniform(0, grid.chunk_w), trnd.uniform(0, grid.chunk_h)
            d.rectangle([x, y, x + 60 * s_zoom, y + 40 * s_zoom], fill=(120, 165, 80))
        for _ in range(900):
            x, y = trnd.uniform(0, grid.chunk_w), trnd.uniform(0, grid.chunk_h)
            r = trnd.uniform(2.5, 9) * s_zoom
            for k in range(14):
                ox, oy = trnd.uniform(-r, r) * 0.6, trnd.uniform(-r, r) * 0.6
                rr = r * trnd.uniform(0.3, 0.6)
                shade = trnd.uniform(0.5, 1.1)
                d.ellipse([x + ox - rr, y + oy - rr, x + ox + rr, y + oy + rr],
                          fill=(int(45 * shade), int(85 * shade), int(38 * shade)))
        im.save(os.path.join(out, "data", "imagery", c.name + ".jpg"), quality=90)
    print(f"synthetic data in {out}: {len(els)} elements, {len(grid.chunks)} chunks")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hxp-test")
