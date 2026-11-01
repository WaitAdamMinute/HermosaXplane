"""Assemble the X-Plane scenery pack."""
import math
import os
import shutil
import zipfile

from PIL import Image, ImageDraw
from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.prepared import prep

from . import buildings as B
from . import lights as L
from . import ortho
from . import beach as BE
from .roofs import RoofClassifier
from . import trees as T
from .dsf import DSFText, compile_dsf, find_dsftool
from .geo import LocalFrame, merc_px, tile_name
from .imagery import Grid, load_chunk_image
from .osm import OSMData, land_polygon
from .props import PropLibrary, paint_atlas
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
        tex = ortho.compose_texture(photo, mask, base, c, atlas.alpha)
        save_texture(tex, os.path.join(tex_dir, c.name), fmt, alpha=True, flip=flip)
        preview_parts.append((c, photo.resize((max(1, photo.size[0] // 4), max(1, photo.size[1] // 4)))))
        polys = ortho.draped_polygons(c, frame, coverage)
        if not polys:
            continue
        pol_rel = f"objects/ortho/{c.name}.pol"
        ortho.write_pol(os.path.join(pack, pol_rel), f"../../textures/{c.name}.{ext}", c)
        idx = dsf.pol_def(pol_rel)
        for windings in polys:
            dsf.polygon_uv(idx, windings)
        print(f"  {c.name}: {len(polys)} draped polygon(s)")

    # ------------------------------------------------------- beach features
    beach_feats = BE.collect(osm, frame, area_poly, os.path.join(data_dir, "beach_lacounty.json"))

    # ------------------------------------------------------------ buildings
    bcfg = dict(cfg["buildings"], _tex_ext=ext)
    placed_b = []
    inside = prep(area_poly)
    if bcfg["enabled"]:
        print("Buildings ...")
        items = [(i, t, p) for i, t, p in osm.buildings() if inside.contains(p.centroid)]
        items = BE.drop_tower_footprints(items, beach_feats)
        ctx = {"coast": unary_union(coast) if coast else None, "alleys": osm.alleys()}
        if bcfg.get("roof_shapes", True):
            print("  classifying roof shapes from the photo ...")
            ctx["roof"] = RoofClassifier(grid, img_dir, frame, bcfg)
        placed_b = B.make_objects(items, frame, grid, atlas, bcfg,
                                  os.path.join(pack, "objects", "buildings"),
                                  "../../textures", f"../../textures/{os.path.basename(lit_path)}", ctx=ctx)
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

    # ---------------------------------------------------- trees and lights
    lib = PropLibrary(pack, "../../textures", ext)
    building_polys = [p for _, _, p in osm.buildings()]
    pier_polys = [p for _, _, p in osm.piers(cfg["pier"]["default_width"]) if inside.intersects(p)]
    tcfg = cfg.get("trees", {})
    n_trees = 0
    if tcfg.get("enabled", True):
        print("Trees ...")
        veg_dir = os.path.join(data_dir, "veg")
        os.makedirs(veg_dir, exist_ok=True)
        lariac = os.path.join(veg_dir, "lariac_trees.json")
        if tcfg.get("tree_source", "lariac") == "lariac" and os.path.exists(lariac):
            crowns = T.lariac_crowns(lariac, frame, tcfg)
        else:
            nrg = os.path.join(veg_dir, "nrg.jpg")
            if not os.path.exists(nrg):
                print("  no NAIP infrared mosaic - using RGB vegetation index from the photo")
                nrg = os.path.join(veg_dir, "rgb_veg.jpg")
                T.rgb_fallback(grid, img_dir, area, nrg)
            crowns = T.detect(nrg, area, frame, building_polys + pier_polys, land, tcfg)
        roads_geom = L.roads_union(osm, area_poly)
        coast_geom = unary_union(coast) if coast else None
        placed_t = T.place(crowns, T.osm_trees(osm), roads_geom, coast_geom, lib, tcfg, frame)
        for obj, lon, lat, hd in placed_t:
            dsf.obj(dsf.obj_def(obj), lon, lat, hd)
        n_trees = len(placed_t)
    n_lights = 0
    if cfg.get("lights", {}).get("enabled", True):
        print("Street lights ...")
        placed_l = L.place(osm, area_poly, building_polys, lib, frame, pier_polys, cfg["pier"])
        for obj, lon, lat, hd in placed_l:
            dsf.obj(dsf.obj_def(obj), lon, lat, hd)
        n_lights = len(placed_l)
    if cfg.get("beach", {}).get("enabled", True):
        print("Beach details ...")
        coast_geom = unary_union(coast) if coast else None
        for obj, lon, lat, hd in BE.place(beach_feats, lib, frame, coast_geom):
            dsf.obj(dsf.obj_def(obj), lon, lat, hd)
    if lib.written:
        print(f"  {len(lib.written)} prop models; writing prop textures ...")
        pim, plit = paint_atlas()
        save_texture(pim, os.path.join(tex_dir, "props"), fmt, alpha=True, flip=flip)
        save_texture(plit, os.path.join(tex_dir, "props_LIT"), fmt, alpha=False, flip=flip)

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

    packs = [pack]
    rcfg = cfg.get("traffic", {})
    if rcfg.get("enabled") and rcfg.get("types"):
        print("Traffic network pack ...")
        tpack = os.path.join(out_root, rcfg["folder"])
        if os.path.exists(tpack):
            shutil.rmtree(tpack)
        tdsf = DSFText(math.floor(lon_c), math.floor(lat_c))
        add_roads(tdsf, osm, frame, area_poly, rcfg)
        ttxt = os.path.join(out_root, f"{tile}_traffic.txt")
        with open(ttxt, "w", newline="\n") as f:
            f.write(tdsf.text())
        if tool:
            compile_dsf(tool, ttxt, os.path.join(tpack, "Earth nav data", tile_dir, f"{tile}.dsf"))
        os.makedirs(tpack, exist_ok=True)
        with open(os.path.join(tpack, "README.txt"), "w", newline="\n") as f:
            f.write(TRAFFIC_README)
        packs.append(tpack)

    write_preview(preview_parts, grid, osm, frame, area_poly, coverage, os.path.join(out_root, "preview.jpg"))
    write_pack_readme(pack, cfg, len(placed_b), grid, n_trees, n_lights)
    if tool:
        zpath = os.path.join(out_root, cfg["pack"]["folder"] + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for pk in packs:
                for r, _, files in os.walk(pk):
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
        hw = tags["highway"].replace("_link", "")
        if hw == "service" and tags.get("service") in ("alley", "parking_aisle", "driveway"):
            continue
        oneway = tags.get("oneway") in ("yes", "1", "-1", "true") or tags.get("junction") == "roundabout"
        t = types.get(f"{hw}_oneway") if oneway else None
        t = t if t is not None else types.get(hw)
        if t is None:
            continue
        nodes = [n for n in nodes if n in osm.nodes]
        if tags.get("oneway") == "-1":
            nodes = nodes[::-1]
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


def write_pack_readme(pack, cfg, n_buildings, grid, n_trees=0, n_lights=0):
    with open(os.path.join(pack, "README.txt"), "w", newline="\n") as f:
        f.write(f"""{cfg['pack']['name']}
{'=' * len(cfg['pack']['name'])}

Photoreal overlay scenery for Hermosa Beach, California (X-Plane 11 / 12).

* Aerial photo draped over the city, beach, surf zone and pier
  ({grid.nx * grid.ny} textures at Web-Mercator zoom {grid.zoom}).
* {n_buildings} buildings extruded from real footprints and heights
  (OpenStreetMap / LA County LARIAC), roofs textured from the photo.
* {n_trees} 3D trees (palms, broadleaf, cypress, shrubs) placed on the
  tree crowns detected in the aerial imagery.
* {n_lights} street and pier lights that glow at night.
* Default autogen, roads, beaches, forests and objects are excluded inside
  the area so nothing misaligned is drawn on top of the photo.

Install: copy this folder into "X-Plane/Custom Scenery/" and start X-Plane.
It must be listed ABOVE any global/ortho mesh packs in
Custom Scenery/scenery_packs.ini (new packs are added at the top).

Data: (c) OpenStreetMap contributors, ODbL.  Tree positions and heights:
County of Los Angeles, LARIAC7 Tree Canopy Tops 2023 (used under the LARIAC
terms of use; no endorsement by the County is implied).  Imagery source: {cfg['imagery']['source']}
(NAIP imagery is public domain, courtesy USDA Farm Production and Conservation).
""")


TRAFFIC_README = """Hermosa Beach traffic network (optional, experimental)
=======================================================

Adds X-Plane's own road network (moving cars, X-Plane street lights) along
every OpenStreetMap road in Hermosa Beach, aligned with the photo.

Install next to zzz_Hermosa_Beach_Photoreal.  In Custom Scenery/scenery_packs.ini
this pack must be listed ABOVE zzz_Hermosa_Beach_Photoreal (the photo pack
removes the default, misaligned roads from packs below it).

If roads look wrong (wrong widths, rails or power lines instead of streets),
delete this folder: the road type numbers it uses could not be verified
against your X-Plane version.  Report it and they can be re-mapped in
config.toml [traffic.types].
"""
