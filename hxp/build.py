"""Assemble the X-Plane scenery pack."""
import math
import os
import shutil
import zipfile

from PIL import Image, ImageDraw
from shapely.geometry import Point, box
from shapely.prepared import prep

from . import buildings as B
from . import ortho
from .dsf import DSFText, compile_dsf, find_dsftool
from .geo import LocalFrame, merc_px, tile_name
from .imagery import Grid, load_chunk_image
from .osm import OSMData, land_polygon
from .textures import WallAtlas, make_lit_texture, save_texture

EXCL_KEYS = {"objects": "obj", "facades": "fac", "forests": "for", "lines": "lin",
             "strings": "str", "polygons": "pol", "roads": "net", "beaches": "bch"}


def build(cfg, root):
    area = cfg["area"]
    icfg = cfg["imagery"]
    data_dir = os.path.join(root, "data")
    out_root = os.path.join(root, "build")
    pack = os.path.join(out_root, cfg["pack"]["folder"])
    if os.path.exists(pack):
        shutil.rmtree(pack)
    tex_dir = os.path.join(pack, "textures")
    os.makedirs(tex_dir)
    fmt = icfg["texture_format"]
    ext = "dds" if fmt == "dds" else "png"
    flip = icfg.get("dds_flip", True)

    lon_c, lat_c = (area["west"] + area["east"]) / 2, (area["south"] + area["north"]) / 2
    frame = LocalFrame(lon_c, lat_c)
    x0, y0 = frame.to_xy(area["west"], area["south"])
    x1, y1 = frame.to_xy(area["east"], area["north"])
    area_poly = box(x0, y0, x1, y1)

    print("Reading OSM ...")
    osm = OSMData(os.path.join(data_dir, "osm.json"), frame)
    big = area_poly.buffer(icfg["sea_opaque"] + icfg["sea_fade"] + 500, join_style=2)
    coast = osm.coastlines()
    land = land_polygon(coast, big) if coast else None
    print(f"  coastline pieces: {len(coast)}; land polygon: {'yes' if land is not None else 'none (all land)'}")
    if land is not None:
        coverage = area_poly.intersection(land.buffer(icfg["sea_opaque"] + icfg["sea_fade"]))
    else:
        coverage = area_poly

    grid = Grid(area, icfg)
    atlas = WallAtlas(grid.tex, grid.strip)
    base = Image.new("RGB", (grid.tex, grid.tex), (128, 128, 128))
    lit_dummy = Image.new("RGB", (grid.tex, grid.tex))
    atlas.rng.seed(7)
    atlas.render(base, lit_dummy)
    print("Writing night-lighting texture ...")
    lit_path = save_texture(make_lit_texture(atlas), os.path.join(tex_dir, "lit"), fmt, alpha=False, flip=flip)

    tile_dir, tile = tile_name(lon_c, lat_c)
    dsf = DSFText(math.floor(lon_c), math.floor(lat_c))
    ex = cfg["exclusions"]
    for k, short in EXCL_KEYS.items():
        if ex.get(k):
            dsf.exclude(short, area["west"], area["south"], area["east"], area["north"])

    # ---------------------------------------------------------------- photo
    print(f"Orthophoto: {grid.nx}x{grid.ny} chunks at zoom {grid.zoom} ...")
    pol_dir = os.path.join(pack, "objects", "ortho")
    os.makedirs(pol_dir)
    img_dir = os.path.join(data_dir, "imagery")
    preview_parts = []
    for c in grid.chunks:
        photo = load_chunk_image(c, img_dir, icfg)
        mask = ortho.alpha_mask(c, frame, land, icfg)
        tex = ortho.compose_texture(photo, mask, base, c)
        save_texture(tex, os.path.join(tex_dir, c.name), fmt, alpha=True, flip=flip)
        preview_parts.append((c, photo))
        polys = ortho.draped_polygons(c, frame, coverage)
        if not polys:
            continue
        pol_rel = f"objects/ortho/{c.name}.pol"
        ortho.write_pol(os.path.join(pack, pol_rel), f"../../textures/{c.name}.{ext}", c)
        idx = dsf.pol_def(pol_rel)
        for windings in polys:
            dsf.polygon_uv(idx, windings)
        print(f"  {c.name}: {len(polys)} draped polygon(s)")

    # ------------------------------------------------------------ buildings
    bcfg = dict(cfg["buildings"], _tex_ext=ext)
    placed_b = []
    inside = prep(area_poly)
    if bcfg["enabled"]:
        print("Buildings ...")
        items = [(i, t, p) for i, t, p in osm.buildings() if inside.contains(p.centroid)]
        placed_b = B.make_objects(items, frame, grid, atlas, bcfg,
                                  os.path.join(pack, "objects", "buildings"),
                                  "../../textures", f"../../textures/{os.path.basename(lit_path)}")
        for name, lon, lat in placed_b:
            dsf.obj(dsf.obj_def(f"objects/buildings/{name}"), lon, lat)

    if cfg["pier"]["enabled"]:
        piers = [(i, t, p) for i, t, p in osm.piers(cfg["pier"]["default_width"]) if inside.intersects(p)]
        if piers:
            print("Pier ...")
            pc = dict(bcfg, min_area=20.0)
            placed = B.make_objects(piers, frame, grid, atlas, pc, os.path.join(pack, "objects", "pier"),
                                    "../../textures", f"../../textures/{os.path.basename(lit_path)}",
                                    kind="pier", pier_cfg=cfg["pier"])
            for name, lon, lat in placed:
                dsf.obj(dsf.obj_def(f"objects/pier/{name}"), lon, lat)

    # ---------------------------------------------------------------- roads
    rcfg = cfg.get("roads", {})
    if rcfg.get("enabled") and rcfg.get("types"):
        add_roads(dsf, osm, frame, area_poly, rcfg)

    # ------------------------------------------------------------------ DSF
    txt_path = os.path.join(out_root, f"{tile}.txt")
    with open(txt_path, "w", newline="\n") as f:
        f.write(dsf.text())
    dsf_path = os.path.join(pack, "Earth nav data", tile_dir, f"{tile}.dsf")
    tool = find_dsftool(cfg["tools"].get("dsftool"), os.path.join(root, "tools"))
    if tool:
        print(f"Compiling DSF with {tool} ...")
        compile_dsf(tool, txt_path, dsf_path)
    else:
        print("!! DSFTool not found - DSF text written to", txt_path)
        print("!! run:  python -m hxp tools   (or set [tools] dsftool) and then build again")

    write_preview(preview_parts, grid, osm, frame, area_poly, coverage, os.path.join(out_root, "preview.jpg"))
    write_pack_readme(pack, cfg, len(placed_b), grid)
    if tool:
        zpath = os.path.join(out_root, cfg["pack"]["folder"] + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for r, _, files in os.walk(pack):
                for fn in files:
                    p = os.path.join(r, fn)
                    z.write(p, os.path.relpath(p, out_root))
        print(f"Done: {pack}\n      {zpath}")
    return pack


def add_roads(dsf, osm, frame, area_poly, rcfg):
    net = dsf.net_def(rcfg["network"])
    types = rcfg["types"]
    inside = prep(area_poly)
    use = {}
    ways = []
    for wid, tags, nodes in osm.highways():
        t = types.get(tags["highway"])
        if t is None:
            continue
        nodes = [n for n in nodes if n in osm.nodes]
        if len(nodes) >= 2:
            ways.append((t, nodes))
            for n in nodes:
                use[n] = use.get(n, 0) + 1
    ids = {}

    def nid(n):
        if n not in ids:
            ids[n] = len(ids) + 1
        return ids[n]

    count = 0
    for t, nodes in ways:
        run = []
        for k, n in enumerate(nodes):
            x, y = osm.nodes[n]
            if not inside.contains(Point(x, y)):
                if len(run) >= 2:
                    _emit(dsf, net, t, run, osm, frame, nid)
                    count += 1
                run = []
                continue
            run.append(n)
            if len(run) >= 2 and (use[n] > 1 or k == len(nodes) - 1) and k != 0:
                _emit(dsf, net, t, run, osm, frame, nid)
                count += 1
                run = [n]
        if len(run) >= 2:
            _emit(dsf, net, t, run, osm, frame, nid)
            count += 1
    print(f"  roads: {count} segments")


def _emit(dsf, net, subtype, run, osm, frame, nid):
    pts = []
    for k, n in enumerate(run):
        lon, lat = frame.to_ll(*osm.nodes[n])
        pts.append((nid(n) if k in (0, len(run) - 1) else None, lon, lat))
    dsf.segment(net, subtype, pts)


def write_preview(parts, grid, osm, frame, area_poly, coverage, path):
    """Top-down check image: photo mosaic + building footprints outlined."""
    scale = 0.25
    W, H = int(grid.nx * grid.chunk_w * scale), int(grid.ny * grid.chunk_h * scale)
    im = Image.new("RGB", (W, H), (20, 40, 70))
    for c, photo in parts:
        sm = photo.resize((int(grid.chunk_w * scale), int(grid.chunk_h * scale)))
        im.paste(sm, (int((c.px0 - grid.px0) * scale), int((c.py0 - grid.py0) * scale)))
    d = ImageDraw.Draw(im)

    def P(x, y):
        lon, lat = frame.to_ll(x, y)
        px, py = merc_px(lon, lat, grid.zoom)
        return (px - grid.px0) * scale, (py - grid.py0) * scale

    for _, _, p in osm.buildings():
        d.line([P(x, y) for x, y in p.exterior.coords], fill=(255, 60, 60), width=1)
    for g in [coverage]:
        for poly in [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", [])):
            if poly.geom_type == "Polygon":
                d.line([P(x, y) for x, y in poly.exterior.coords], fill=(255, 255, 0), width=2)
    for line in osm.coastlines():
        d.line([P(x, y) for x, y in line.coords], fill=(0, 200, 255), width=2)
    im.save(path, quality=88)
    print(f"  preview: {path}")


def write_pack_readme(pack, cfg, n_buildings, grid):
    with open(os.path.join(pack, "README.txt"), "w", newline="\n") as f:
        f.write(f"""{cfg['pack']['name']}
{'=' * len(cfg['pack']['name'])}

Photoreal overlay scenery for Hermosa Beach, California (X-Plane 11 / 12).

* Aerial photo draped over the city, beach, surf zone and pier
  ({grid.nx * grid.ny} textures at Web-Mercator zoom {grid.zoom}).
* {n_buildings} buildings extruded from real footprints and heights
  (OpenStreetMap / LA County LARIAC), roofs textured from the photo.
* Default autogen, roads, beaches, forests and objects are excluded inside
  the area so nothing misaligned is drawn on top of the photo.

Install: copy this folder into "X-Plane/Custom Scenery/" and start X-Plane.
It must be listed ABOVE any global/ortho mesh packs in
Custom Scenery/scenery_packs.ini (new packs are added at the top).

Data: (c) OpenStreetMap contributors, ODbL.  Imagery source: {cfg['imagery']['source']}
(NAIP imagery is public domain, courtesy USDA Farm Production and Conservation).
""")
