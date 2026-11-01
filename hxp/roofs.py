"""Decide each building's roof shape from the aerial photo.

There is no roof-shape attribute in the County or OSM building data for
Hermosa Beach, so it is read off the imagery:

* A pitched roof lit by the sun shows two halves of clearly different
  brightness on either side of the ridge; a flat roof is evenly lit.
* Clay-tile roofs (orange/red) are always pitched.
* A hip roof also shows contrast between its two end facets; a gable doesn't.

OSM `roof:shape` tags, where present, always win.
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw

from .geo import merc_px

SAMPLE_ZOOM = 18
PITCHED_TYPES = {"house", "detached", "residential", "semidetached_house", "terrace", "yes", "bungalow",
                 "church", "chapel", "garage", "cabin"}
OSM_SHAPES = {"gabled": "gable", "hipped": "hip", "pyramidal": "hip", "half-hipped": "hip",
              "flat": "flat", "skillion": "flat", "mansard": "hip", "gambrel": "gable"}


class RoofClassifier:
    def __init__(self, grid, img_dir, frame, cfg):
        self.frame = frame
        self.cfg = cfg
        self.scale = 2.0 ** (SAMPLE_ZOOM - grid.zoom)
        z = SAMPLE_ZOOM
        self.px0 = math.floor(grid.px0 * self.scale)
        self.py0 = math.floor(grid.py0 * self.scale)
        w = int(math.ceil(grid.nx * grid.chunk_w * self.scale))
        h = int(math.ceil(grid.ny * grid.chunk_h * self.scale))
        mos = Image.new("RGB", (w, h))
        for c in grid.chunks:
            fn = os.path.join(img_dir, c.name + ".jpg")
            if not os.path.exists(fn):
                continue
            im = Image.open(fn).convert("RGB")
            im = im.resize((max(1, int(im.size[0] * self.scale)), max(1, int(im.size[1] * self.scale))), Image.BOX)
            mos.paste(im, (int(c.px0 * self.scale) - self.px0, int(c.py0 * self.scale) - self.py0))
        self.rgb = np.asarray(mos).astype(np.float32)
        self.lum = self.rgb @ np.array([0.299, 0.587, 0.114], np.float32)
        self.zoom = z
        self.counts = {}

    def _pixels(self, poly):
        """Pixel centres (world x, y) and values inside the footprint shrunk by 0.6 m."""
        p = poly.buffer(-0.6)
        if p.is_empty or p.geom_type != "Polygon":
            return None
        pts = []
        for x, y in p.exterior.coords:
            lon, lat = self.frame.to_ll(x, y)
            px, py = merc_px(lon, lat, self.zoom)
            pts.append((px - self.px0, py - self.py0))
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        x0, y0 = max(0, int(min(xs)) - 1), max(0, int(min(ys)) - 1)
        x1, y1 = min(self.lum.shape[1], int(max(xs)) + 2), min(self.lum.shape[0], int(max(ys)) + 2)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        m = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(m).polygon([(x - x0, y - y0) for x, y in pts], fill=255)
        mask = np.asarray(m) > 0
        if mask.sum() < 12:
            return None
        iy, ix = np.nonzero(mask)
        # pixel centre -> world metres (inverse of the projection, linearised locally)
        cx, cy = poly.centroid.x, poly.centroid.y
        lon, lat = self.frame.to_ll(cx, cy)
        pcx, pcy = merc_px(lon, lat, self.zoom)
        mpp = 2 * math.pi * 6378137.0 * math.cos(math.radians(lat)) / (256 * 2 ** self.zoom)
        wx = cx + ((ix + x0 + 0.5) - (pcx - self.px0)) * mpp
        wy = cy - ((iy + y0 + 0.5) - (pcy - self.py0)) * mpp
        return wx, wy, self.lum[iy + y0, ix + x0], self.rgb[iy + y0, ix + x0]

    def __call__(self, poly, tags, height):
        spec = self._classify(poly, tags, height)
        self.counts[spec["kind"]] = self.counts.get(spec["kind"], 0) + 1
        return spec

    def _classify(self, poly, tags, height):
        shape = tags.get("roof:shape")
        if shape in OSM_SHAPES:
            return {"kind": OSM_SHAPES[shape], "pitch": 24}
        b = tags.get("building", "yes")
        if b not in PITCHED_TYPES or height > self.cfg["max_pitched_height"] or not (20 <= poly.area <= 900):
            return {"kind": "flat"}
        got = self._pixels(poly)
        if got is None:
            return {"kind": "flat"}
        wx, wy, lum, rgb = got
        mrr = poly.minimum_rotated_rectangle
        c = list(mrr.exterior.coords)[:4]
        e0 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
        e1 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
        if math.hypot(*e0) < math.hypot(*e1):
            e0 = e1
        L = math.hypot(*e0)
        u = (e0[0] / L, e0[1] / L)
        cx, cy = mrr.centroid.x, mrr.centroid.y
        du = (wx - cx) * u[0] + (wy - cy) * u[1]
        dv = -(wx - cx) * u[1] + (wy - cy) * u[0]
        mean = max(float(lum.mean()), 1.0)

        def contrast(sel_a, sel_b):
            if sel_a.sum() < 4 or sel_b.sum() < 4:
                return 0.0
            return abs(float(lum[sel_a].mean()) - float(lum[sel_b].mean())) / mean

        c_across = contrast(dv < -0.3, dv > 0.3)        # two sides of a ridge along the long axis
        c_along = contrast(du < -0.25 * L, du > 0.25 * L)  # the two hip ends
        r, g, bl = (float(rgb[:, k].mean()) for k in range(3))
        tile = (r - g) > 16 and (r - bl) > 34 and r > 110
        thr = self.cfg["pitch_contrast"]
        pitched = tile or c_across > thr or c_along > thr * 1.2
        if not pitched:
            return {"kind": "flat"}
        rect = poly.area / max(mrr.area, 1e-6) >= 0.88
        if rect and c_along < thr * 0.6 and not tile:
            kind = "gable"
        else:
            kind = "hip"
        return {"kind": kind, "pitch": 22 if tile else 26}
