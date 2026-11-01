"""Orthophoto overlay: per-chunk textures, sea alpha mask, draped polygons."""
from PIL import Image, ImageDraw, ImageFilter
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from .geo import merc_px


def _rings_px(geom, chunk, frame, scale):
    """Yield (exterior_px, [holes_px]) for geometry in chunk-local pixel space."""
    z = chunk.grid.zoom
    polys = [geom] if isinstance(geom, Polygon) else list(getattr(geom, "geoms", []))
    for p in polys:
        if p.is_empty or p.geom_type != "Polygon":
            continue

        def conv(ring):
            out = []
            for x, y in ring.coords:
                lon, lat = frame.to_ll(x, y)
                px, py = merc_px(lon, lat, z)
                out.append(((px - chunk.px0) * scale, (py - chunk.py0) * scale))
            return out

        yield conv(p.exterior), [conv(r) for r in p.interiors]


def alpha_mask(chunk, frame, land, cfg):
    """L-mode mask: 255 on land and within sea_opaque of it, fading to 0."""
    g = chunk.grid
    scale = 0.125
    w, h = int(g.chunk_w * scale), int(g.chunk_h * scale)
    if land is None:
        return Image.new("L", (g.chunk_w, g.chunk_h), 255)
    opaque, fade = cfg["sea_opaque"], cfg["sea_fade"]
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    steps = 16
    for k in range(steps, -1, -1):
        dist = opaque + fade * k / steps
        a = int(round(255 * (1 - k / steps)))
        for ext, holes in _rings_px(land.buffer(dist, resolution=8), chunk, frame, scale):
            d.polygon(ext, fill=a)
            for hole in holes:
                d.polygon(hole, fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    return mask.resize((g.chunk_w, g.chunk_h), Image.BILINEAR)


def chunk_footprint(chunk, frame):
    w, s, e, n = chunk.lonlat_bounds()
    x0, y0 = frame.to_xy(w, s)
    x1, y1 = frame.to_xy(e, n)
    return box(x0, y0, x1, y1)


def draped_polygons(chunk, frame, coverage):
    """DSF polygon windings [(lon, lat, s, t), ...] for this chunk."""
    g = chunk_footprint(chunk, frame).intersection(coverage)
    if g.is_empty:
        return []
    g = g.simplify(0.5)
    parts = [g] if isinstance(g, Polygon) else [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    out = []
    for p in parts:
        if p.area < 10:
            continue
        p = orient(p, sign=1.0)
        windings = []
        for ring in [p.exterior] + list(p.interiors):
            pts = []
            for x, y in list(ring.coords)[:-1]:
                lon, lat = frame.to_ll(x, y)
                u, v = chunk.uv_clamped(lon, lat)
                pts.append((lon, lat, u, v))
            windings.append(pts)
        out.append(windings)
    return out


def write_pol(path, texture_rel, chunk):
    w, s, e, n = chunk.lonlat_bounds()
    lat, lon = (s + n) / 2, (w + e) / 2
    size_m = (n - s) * 111320.0
    radius = size_m * 0.75
    with open(path, "w", newline="\n") as f:
        f.write("A\n850\nDRAPED_POLYGON\n\n")
        f.write(f"TEXTURE_NOWRAP {texture_rel}\n")
        f.write("SCALE 1000 1000\n")
        f.write(f"LOAD_CENTER {lat:.6f} {lon:.6f} {radius:.0f} {chunk.grid.tex}\n")
        f.write("LAYER_GROUP beaches 1\n")


def compose_texture(photo, mask, atlas_img, chunk, atlas_alpha=None):
    """Photo (top) + wall atlas strip (bottom), with alpha."""
    g = chunk.grid
    tex = atlas_img.copy()  # RGB, full size, strip already drawn
    tex.paste(photo, (0, 0))
    a = atlas_alpha.copy() if atlas_alpha is not None else Image.new("L", (g.tex, g.tex), 255)
    a.paste(mask, (0, 0))
    tex.putalpha(a)
    return tex
