"""Photo grid definition and imagery download.

The area is covered by a grid of "chunks" in global Web-Mercator pixel space
at the configured zoom.  Each chunk becomes one texture: the top part holds
the photo, the bottom `wall_strip` rows hold the building wall atlas.
"""
import io
import math
import os
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageEnhance

from .geo import merc_px, px_lonlat, px_to_merc_m

USER_AGENT = "HermosaXplane/1.0 (X-Plane scenery builder)"
Image.MAX_IMAGE_PIXELS = None


class Chunk:
    def __init__(self, grid, i, j):
        self.grid, self.i, self.j = grid, i, j
        self.px0 = grid.px0 + i * grid.chunk_w
        self.py0 = grid.py0 + j * grid.chunk_h
        self.px1 = self.px0 + grid.chunk_w
        self.py1 = self.py0 + grid.chunk_h
        self.name = f"ortho_{j:02d}_{i:02d}"

    def lonlat_bounds(self):
        w, n = px_lonlat(self.px0, self.py0, self.grid.zoom)
        e, s = px_lonlat(self.px1, self.py1, self.grid.zoom)
        return w, s, e, n

    def uv(self, lon, lat):
        """Texture coordinates (X-Plane: origin bottom-left) of a point."""
        x, y = merc_px(lon, lat, self.grid.zoom)
        t = self.grid.tex
        u = (x - self.px0) / t
        v = 1.0 - (y - self.py0) / t
        return u, v

    def uv_clamped(self, lon, lat):
        u, v = self.uv(lon, lat)
        g = self.grid
        vmin = 1.0 - g.chunk_h / g.tex
        half = 0.5 / g.tex
        return min(max(u, half), 1 - half), min(max(v, vmin + half), 1 - half)


class Grid:
    def __init__(self, area, img_cfg):
        self.zoom = int(img_cfg["zoom"])
        self.tex = int(img_cfg["texture_size"])
        self.strip = int(img_cfg["wall_strip"])
        self.chunk_w = self.tex
        self.chunk_h = self.tex - self.strip
        x0, y0 = merc_px(area["west"], area["north"], self.zoom)
        x1, y1 = merc_px(area["east"], area["south"], self.zoom)
        self.px0, self.py0 = math.floor(x0), math.floor(y0)
        self.nx = math.ceil((x1 - self.px0) / self.chunk_w)
        self.ny = math.ceil((y1 - self.py0) / self.chunk_h)
        self.chunks = [Chunk(self, i, j) for j in range(self.ny) for i in range(self.nx)]

    def chunk_for(self, lon, lat):
        x, y = merc_px(lon, lat, self.zoom)
        i = int((x - self.px0) // self.chunk_w)
        j = int((y - self.py0) // self.chunk_h)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return self.chunks[j * self.nx + i]
        return None


def _get(url, tries=4):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                ctype = r.headers.get("Content-Type", "")
                data = r.read()
            if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
                raise RuntimeError(f"not an image ({ctype}): {data[:200]!r}")
            return data
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(2 ** (k + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


def _fetch_arcgis(chunk, url, cache, tag, piece=2048):
    """Any ArcGIS ImageServer (.../exportImage) or MapServer (.../export) endpoint."""
    g = chunk.grid
    out = Image.new("RGB", (g.chunk_w, g.chunk_h))
    step = piece
    for oy in range(0, g.chunk_h, step):
        for ox in range(0, g.chunk_w, step):
            w = min(step, g.chunk_w - ox)
            h = min(step, g.chunk_h - oy)
            fn = os.path.join(cache, f"{tag}_z{g.zoom}_{chunk.px0 + ox}_{chunk.py0 + oy}_{w}x{h}.jpg")
            if not os.path.exists(fn):
                xmin, ymax = px_to_merc_m(chunk.px0 + ox, chunk.py0 + oy, g.zoom)
                xmax, ymin = px_to_merc_m(chunk.px0 + ox + w, chunk.py0 + oy + h, g.zoom)
                q = urllib.parse.urlencode({
                    "bbox": f"{xmin},{ymin},{xmax},{ymax}",
                    "bboxSR": 3857, "imageSR": 3857, "size": f"{w},{h}",
                    "format": "jpg", "compressionQuality": 92,
                    "interpolation": "RSP_BilinearInterpolation", "f": "image",
                })
                data = _get(f"{url}?{q}")
                with open(fn, "wb") as f:
                    f.write(data)
            im = Image.open(fn).convert("RGB")
            if im.size != (w, h):
                im = im.resize((w, h), Image.LANCZOS)
            out.paste(im, (ox, oy))
    return out


def _fetch_xyz(chunk, cfg, cache):
    g = chunk.grid
    out = Image.new("RGB", (g.chunk_w, g.chunk_h))
    tx0, ty0 = chunk.px0 // 256, chunk.py0 // 256
    tx1, ty1 = (chunk.px1 - 1) // 256, (chunk.py1 - 1) // 256
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            fn = os.path.join(cache, f"xyz_z{g.zoom}_{tx}_{ty}.img")
            if not os.path.exists(fn):
                url = cfg["xyz_url"].format(z=g.zoom, x=tx, y=ty)
                data = _get(url)
                with open(fn, "wb") as f:
                    f.write(data)
            im = Image.open(fn).convert("RGB")
            if im.size != (256, 256):
                im = im.resize((256, 256), Image.LANCZOS)
            out.paste(im, (tx * 256 - chunk.px0, ty * 256 - chunk.py0))
    return out


def fetch(grid, cfg, out_dir, cache_dir):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    src = cfg["source"]
    for c in grid.chunks:
        dst = os.path.join(out_dir, c.name + ".jpg")
        if os.path.exists(dst):
            print(f"  {c.name}: cached")
            continue
        print(f"  {c.name}: downloading ({src}) bounds={tuple(round(v, 5) for v in c.lonlat_bounds())}")
        if src == "naip":
            im = _fetch_arcgis(c, cfg["naip_url"], cache_dir, "naip")
        elif src == "arcgis":
            im = _fetch_arcgis(c, cfg["arcgis_url"], cache_dir, "arcgis", int(cfg.get("arcgis_max_px", 2048)))
        else:
            im = _fetch_xyz(c, cfg, cache_dir)
        im.save(dst, quality=95)


def load_chunk_image(chunk, img_dir, cfg):
    fn = os.path.join(img_dir, chunk.name + ".jpg")
    im = Image.open(fn).convert("RGB")
    g = chunk.grid
    if im.size != (g.chunk_w, g.chunk_h):
        im = im.resize((g.chunk_w, g.chunk_h), Image.LANCZOS)
    if cfg.get("brightness", 1.0) != 1.0:
        im = ImageEnhance.Brightness(im).enhance(cfg["brightness"])
    if cfg.get("contrast", 1.0) != 1.0:
        im = ImageEnhance.Contrast(im).enhance(cfg["contrast"])
    if cfg.get("saturation", 1.0) != 1.0:
        im = ImageEnhance.Color(im).enhance(cfg["saturation"])
    return im


def image_bytes(im, fmt="JPEG"):
    b = io.BytesIO()
    im.save(b, fmt)
    return b.getvalue()
