"""Command line: python -m hxp [fetch|tools|build|all] [--config config.toml]"""
import argparse
import os
import sys
import tomllib

from . import dsf, imagery, osm


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
    if a.command in ("fetch", "fetch-imagery", "all"):
        print("Fetching imagery ...")
        grid = imagery.Grid(cfg["area"], cfg["imagery"])
        imagery.fetch(grid, cfg["imagery"], os.path.join(data, "imagery"), os.path.join(data, "cache"))
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
