"""Find individual trees in aerial imagery and place 3D tree models on them.

Vegetation comes from NAIP's near-infrared band (NDVI).  If that band can't be
downloaded, an RGB vegetation index computed from the photo is used instead.
Tree crowns are told apart from lawns by texture (crowns are full of
self-shadow) and darkness, then split into individual trees with a distance
transform.  Crown size gives the tree size; small isolated crowns along
streets and near the beach are mostly palms in Hermosa Beach.
"""
import math
import os
import random
import urllib.parse

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from shapely.geometry import Point
from shapely.prepared import prep
from shapely.strtree import STRtree

from .geo import merc_px, px_lonlat, px_to_merc_m
from .imagery import _get

VEG_ZOOM = 18


class VegRaster:
    """NIR/R/G raster over the area at VEG_ZOOM (global pixel origin px0, py0)."""

    def __init__(self, area):
        x0, y0 = merc_px(area["west"], area["north"], VEG_ZOOM)
        x1, y1 = merc_px(area["east"], area["south"], VEG_ZOOM)
        self.px0, self.py0 = math.floor(x0), math.floor(y0)
        self.w, self.h = math.ceil(x1) - self.px0, math.ceil(y1) - self.py0
        lat = (area["north"] + area["south"]) / 2
        self.m_per_px = 2 * math.pi * 6378137.0 * math.cos(math.radians(lat)) / (256 * 2 ** VEG_ZOOM)


def fetch_nir(area, naip_url, out_path, cache):
    """Download NAIP false-colour (NIR, R, G) mosaic. Returns True on success."""
    vr = VegRaster(area)
    os.makedirs(cache, exist_ok=True)
    out = Image.new("RGB", (vr.w, vr.h))
    step = 2048
    for oy in range(0, vr.h, step):
        for ox in range(0, vr.w, step):
            w, h = min(step, vr.w - ox), min(step, vr.h - oy)
            fn = os.path.join(cache, f"nrg_z{VEG_ZOOM}_{vr.px0 + ox}_{vr.py0 + oy}_{w}x{h}.jpg")
            if not os.path.exists(fn):
                xmin, ymax = px_to_merc_m(vr.px0 + ox, vr.py0 + oy, VEG_ZOOM)
                xmax, ymin = px_to_merc_m(vr.px0 + ox + w, vr.py0 + oy + h, VEG_ZOOM)
                q = urllib.parse.urlencode({
                    "bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": 3857, "imageSR": 3857,
                    "size": f"{w},{h}", "format": "jpg", "compressionQuality": 95,
                    "bandIds": "3,0,1", "interpolation": "RSP_BilinearInterpolation", "f": "image",
                })
                data = _get(f"{naip_url}?{q}")
                with open(fn, "wb") as f:
                    f.write(data)
            im = Image.open(fn).convert("RGB")
            if im.size != (w, h):
                im = im.resize((w, h), Image.LANCZOS)
            out.paste(im, (ox, oy))
    out.save(out_path, quality=95)
    return True


def rgb_fallback(grid, img_dir, area, out_path):
    """Build a pseudo NIR/R/G raster from the RGB photo (NIR ~ green-boosted)."""
    vr = VegRaster(area)
    scale = 2 ** (VEG_ZOOM - grid.zoom)
    out = Image.new("RGB", (vr.w, vr.h))
    for c in grid.chunks:
        fn = os.path.join(img_dir, c.name + ".jpg")
        im = Image.open(fn).convert("RGB")
        w, h = max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))
        im = im.resize((w, h), Image.LANCZOS)
        a = np.asarray(im).astype(np.float32)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        vari = (g - r) / (g + r - b + 1e-3)
        nir = np.clip(g + 120 * np.clip(vari, -0.2, 0.6), 0, 255)  # synthetic NIR
        nrg = np.stack([nir, r, g], -1).astype(np.uint8)
        out.paste(Image.fromarray(nrg), (int((c.px0 * scale) - vr.px0), int((c.py0 * scale) - vr.py0)))
    out.save(out_path, quality=95)


# --------------------------------------------------------------------------


def detect(nrg_path, area, frame, exclude_geoms, land, cfg):
    """Return list of dict(x, y, r) crowns in local metres."""
    vr = VegRaster(area)
    a = np.asarray(Image.open(nrg_path).convert("RGB")).astype(np.float32)
    nir, red, grn = a[..., 0], a[..., 1], a[..., 2]
    ndvi = (nir - red) / (nir + red + 1e-3)
    vis = (red + grn) / 2

    # Exclusion mask (buildings shrunk a bit so overhanging crowns survive, sea)
    excl = Image.new("L", (vr.w, vr.h), 0)
    d = ImageDraw.Draw(excl)

    def P(x, y):
        lon, lat = frame.to_ll(x, y)
        px, py = merc_px(lon, lat, VEG_ZOOM)
        return px - vr.px0, py - vr.py0

    for g in exclude_geoms:
        g2 = g.buffer(-0.8)
        for p in ([g2] if g2.geom_type == "Polygon" else list(getattr(g2, "geoms", []))):
            if p.is_empty or p.geom_type != "Polygon":
                continue
            d.polygon([P(x, y) for x, y in p.exterior.coords], fill=255)
    if land is not None:
        sea = Image.new("L", (vr.w, vr.h), 255)
        ds = ImageDraw.Draw(sea)
        for p in ([land] if land.geom_type == "Polygon" else list(getattr(land, "geoms", []))):
            ds.polygon([P(x, y) for x, y in p.exterior.coords], fill=0)
            for r in p.interiors:
                ds.polygon([P(x, y) for x, y in r.coords], fill=255)
        excl = Image.fromarray(np.maximum(np.asarray(excl), np.asarray(sea)))
    excl = np.asarray(excl) > 0

    valid = ~excl
    thr = cfg["ndvi_threshold"]
    if thr == "auto":
        v = ndvi[valid]
        thr = float(np.clip(np.percentile(v, 72), 0.08, 0.35)) if v.size else 0.2
    veg = (ndvi > thr) & valid
    # texture: local standard deviation of NIR over ~3.5 m
    k = max(3, int(round(3.5 / vr.m_per_px)) | 1)
    m1 = ndi.uniform_filter(nir, k)
    m2 = ndi.uniform_filter(nir * nir, k)
    std = np.sqrt(np.maximum(m2 - m1 * m1, 0))
    if veg.any():
        s_thr = np.percentile(std[veg], cfg["texture_percentile"])
        v_thr = np.percentile(vis[veg], 45)
    else:
        s_thr, v_thr = 0, 0
    tree = veg & ((std > s_thr) | (vis < v_thr))
    tree = ndi.binary_opening(tree, iterations=1)
    tree = ndi.binary_closing(tree, iterations=1)
    lab, n = ndi.label(tree)
    sizes = ndi.sum(tree, lab, index=np.arange(1, n + 1))
    min_px = cfg["min_crown_area"] / vr.m_per_px ** 2
    keep = np.zeros(n + 1, bool)
    keep[1:] = sizes >= min_px
    tree = keep[lab]
    lab, n = ndi.label(tree)
    blob_area = np.zeros(n + 1)
    blob_area[1:] = ndi.sum(tree, lab, index=np.arange(1, n + 1)) * vr.m_per_px ** 2

    dt = ndi.distance_transform_edt(tree) * vr.m_per_px
    mx = ndi.maximum_filter(dt, size=max(3, int(2.0 / vr.m_per_px) | 1))
    peaks = np.argwhere((dt == mx) & (dt >= cfg["min_crown_radius"]))
    order = np.argsort(-dt[peaks[:, 0], peaks[:, 1]])
    peaks = peaks[order]
    accepted = []
    grid = {}
    cell = 4.0
    max_r = cfg["max_crown_radius"]
    for py, px in peaks:
        r = min(dt[py, px] + 0.5, max_r)
        lon, lat = px_lonlat(px + vr.px0 + 0.5, py + vr.py0 + 0.5, VEG_ZOOM)
        x, y = frame.to_xy(lon, lat)
        gx, gy = int(x // cell), int(y // cell)
        ok = True
        for ix in range(gx - 4, gx + 5):
            for iy in range(gy - 4, gy + 5):
                for (ox, oy, orr) in grid.get((ix, iy), ()):
                    if (ox - x) ** 2 + (oy - y) ** 2 < (0.75 * (orr + r)) ** 2:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            grid.setdefault((gx, gy), []).append((x, y, r))
            accepted.append(dict(x=x, y=y, r=r, blob=float(blob_area[lab[py, px]]),
                                 ndvi=float(ndvi[py, px])))
    print(f"  trees: ndvi>{thr:.2f}, {int(tree.sum() * vr.m_per_px ** 2)} m2 canopy, {len(accepted)} crowns")
    return accepted


def osm_trees(osm):
    out = []
    for nid, (x, y) in osm.nodes.items():
        tags = osm.node_tags.get(nid)
        if tags and tags.get("natural") == "tree":
            out.append((x, y, tags))
    return out


def place(crowns, osm_tree_nodes, roads_geom, coast_geom, lib, cfg, frame, seed=5):
    """Choose species/height for every crown; returns [(obj_path, lon, lat, heading)]."""
    rnd = random.Random(seed)
    out = []
    road_near = prep(roads_geom.buffer(14)) if roads_geom is not None else None
    coast_near = prep(coast_geom.buffer(450)) if coast_geom is not None else None
    tree_pts = [Point(c["x"], c["y"]) for c in crowns]
    tree_idx = STRtree(tree_pts) if tree_pts else None
    for c in crowns:
        r = c["r"]
        pt = Point(c["x"], c["y"])
        streety = road_near is not None and road_near.contains(pt)
        beachy = coast_near is not None and coast_near.contains(pt)
        p_palm = cfg["palm_base"]
        if r <= 3.2 and c["blob"] < 60:
            p_palm += 0.35 * streety + 0.25 * beachy
        if rnd.random() < p_palm:
            h = rnd.uniform(9, 21) if r < 2.6 else rnd.uniform(7, 14)
            kind = "fan" if (r < 2.4 and h > 11) else "date"
            obj = lib.palm(h, rnd, kind)
        elif r < 1.6 and rnd.random() < 0.08:
            obj = lib.cypress(rnd.uniform(6, 13), rnd)
        elif r < 1.3:
            obj = lib.shrub(r * 2.0, rnd)
        else:
            h = max(3.5, min(18.0, (2 * r) * rnd.uniform(1.0, 1.45) + 1.5))
            obj = lib.broadleaf(h, r, rnd)
        lon, lat = frame.to_ll(c["x"], c["y"])
        out.append((obj, lon, lat, rnd.uniform(0, 360)))
    # OSM-mapped trees not already detected
    added = 0
    for x, y, tags in osm_tree_nodes:
        pt = Point(x, y)
        if tree_idx is not None and len(tree_idx.query(pt.buffer(3.0))):
            continue
        genus = (tags.get("genus", "") + tags.get("species", "") + tags.get("taxon", "")).lower()
        if any(k in genus for k in ("washingtonia", "phoenix", "syagrus", "palm", "arecaceae")):
            obj = lib.palm(rnd.uniform(10, 20), rnd)
        elif "cupressus" in genus:
            obj = lib.cypress(rnd.uniform(7, 13), rnd)
        else:
            obj = lib.broadleaf(rnd.uniform(6, 12), rnd.uniform(2.5, 4), rnd)
        lon, lat = frame.to_ll(x, y)
        out.append((obj, lon, lat, rnd.uniform(0, 360)))
        added += 1
    n_palm = sum(1 for o in out if "palm" in o[0])
    print(f"  trees placed: {len(out)} ({n_palm} palms, {added} from OSM tree nodes)")
    return out
