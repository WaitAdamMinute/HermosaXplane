"""Command line: python -m hxp [fetch|tools|build|all] [--config config.toml]"""
import argparse
import os
import sys
import tomllib

from . import beach, dsf, imagery, osm, trees


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hxp", description="Build Hermosa Beach photoreal scenery for X-Plane")
    ap.add_argument("command", choices=["fetch", "fetch-osm", "fetch-imagery", "tools", "build", "all"])
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--root", default=".", help="project directory (data/, build/, tools/)")
    a = ap.parse_args(argv)
    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    root = os.path.abspath(a.root)
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)

    if a.command in ("fetch", "fetch-osm", "all"):
        print("Fetching OpenStreetMap data ...")
        osm.fetch(cfg["area"], os.path.join(data, "osm.json"))
        if cfg.get("trees", {}).get("enabled", True) and cfg["trees"].get("tree_source", "lariac") == "lariac":
            print("Fetching LA County LiDAR tree tops ...")
            os.makedirs(os.path.join(data, "veg"), exist_ok=True)
            try:
                trees.fetch_lariac_trees(cfg["area"], os.path.join(data, "veg", "lariac_trees.json"))
            except Exception as ex:  # noqa: BLE001 - fall back to image-based detection
                print(f"  LARIAC tree tops unavailable ({ex}); trees will be detected from imagery")
        if cfg.get("beach", {}).get("enabled", True):
            print("Fetching LA County lifeguard towers ...")
            try:
                beach.fetch_lacounty_towers(cfg["area"], os.path.join(data, "beach_lacounty.json"))
            except Exception as ex:  # noqa: BLE001 - OSM towers only
                print(f"  LA County lifeguard tower layer unavailable ({ex}); using OSM only")
    if a.command in ("fetch", "fetch-imagery", "all"):
        print("Fetching imagery ...")
        grid = imagery.Grid(cfg["area"], cfg["imagery"])
        imagery.fetch(grid, cfg["imagery"], os.path.join(data, "imagery"), os.path.join(data, "cache"))
        if cfg.get("trees", {}).get("enabled", True):
            print("Fetching NAIP near-infrared for tree detection ...")
            os.makedirs(os.path.join(data, "veg"), exist_ok=True)
            try:
                trees.fetch_nir(cfg["area"], cfg["imagery"]["naip_url"], os.path.join(data, "veg", "nrg.jpg"),
                                os.path.join(data, "cache"))
            except Exception as ex:  # noqa: BLE001 - fall back to RGB index at build time
                print(f"  NIR download failed ({ex}); the build will use an RGB vegetation index")
    if a.command in ("tools", "all"):
        tools = os.path.join(root, "tools")
        if not dsf.find_dsftool(cfg["tools"].get("dsftool"), tools):
            print("Downloading DSFTool ...")
            print("  ->", dsf.download_dsftool(tools))
    if a.command in ("build", "all"):
        from .build import build
        build(cfg, root)


if __name__ == "__main__":
    sys.exit(main())
