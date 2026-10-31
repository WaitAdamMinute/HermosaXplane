"""DSF text generation and compilation with Laminar Research's DSFTool."""
import os
import platform
import re
import shutil
import stat
import subprocess
import urllib.request
import zipfile

XPTOOLS_PAGE = "https://developer.x-plane.com/tools/xptools/"


class DSFText:
    def __init__(self, tile_lon, tile_lat):
        self.props = [
            ("sim/planet", "earth"),
            ("sim/overlay", "1"),
            ("sim/require_object", "1/0"),
            ("sim/require_facade", "1/0"),
            ("sim/creation_agent", "HermosaXplane"),
        ]
        self.tile = (tile_lon, tile_lat)
        self.obj_defs, self.pol_defs, self.net_defs = [], [], []
        self.body = []

    def exclude(self, kind, w, s, e, n):
        self.props.append((f"sim/exclude_{kind}", f"{w:.6f}/{s:.6f}/{e:.6f}/{n:.6f}"))

    def obj_def(self, path):
        self.obj_defs.append(path)
        return len(self.obj_defs) - 1

    def pol_def(self, path):
        self.pol_defs.append(path)
        return len(self.pol_defs) - 1

    def net_def(self, path):
        self.net_defs.append(path)
        return len(self.net_defs) - 1

    def obj(self, idx, lon, lat, heading=0.0):
        self.body.append(f"OBJECT {idx} {lon:.9f} {lat:.9f} {heading:.3f}")

    def polygon_uv(self, idx, windings):
        """Draped polygon with explicit texture coordinates (param 65535)."""
        b = self.body
        b.append(f"BEGIN_POLYGON {idx} 65535 4")
        for w in windings:
            b.append("BEGIN_WINDING")
            for lon, lat, s, t in w:
                b.append(f"POLYGON_POINT {lon:.9f} {lat:.9f} {s:.7f} {t:.7f}")
            b.append("END_WINDING")
        b.append("END_POLYGON")

    def segment(self, net, subtype, nodes):
        """nodes: [(node_id or None, lon, lat)]; first and last must have ids."""
        b = self.body
        (n0, lo, la) = nodes[0]
        b.append(f"BEGIN_SEGMENT {net} {subtype} {n0} {lo:.9f} {la:.9f} 0.000000")
        for _, lo, la in nodes[1:-1]:
            b.append(f"SHAPE_POINT {lo:.9f} {la:.9f} 0.000000")
        (n1, lo, la) = nodes[-1]
        b.append(f"END_SEGMENT {n1} {lo:.9f} {la:.9f} 0.000000")

    def text(self):
        lon, lat = self.tile
        out = ["I", "800", "DSF2TEXT", ""]
        for k, v in self.props:
            out.append(f"PROPERTY {k} {v}")
        out += [f"PROPERTY sim/west {lon}", f"PROPERTY sim/east {lon + 1}",
                f"PROPERTY sim/north {lat + 1}", f"PROPERTY sim/south {lat}", ""]
        out += [f"OBJECT_DEF {p}" for p in self.obj_defs]
        out += [f"POLYGON_DEF {p}" for p in self.pol_defs]
        out += [f"NETWORK_DEF {p}" for p in self.net_defs]
        out.append("")
        out += self.body
        out.append("")
        return "\n".join(out)


# --------------------------------------------------------------------------


def find_dsftool(configured, tools_dir):
    exe = "DSFTool.exe" if os.name == "nt" else "DSFTool"
    if configured and os.path.exists(configured):
        return configured
    for root, _, files in os.walk(tools_dir) if os.path.isdir(tools_dir) else []:
        if exe in files:
            return os.path.join(root, exe)
    return shutil.which("DSFTool") or shutil.which(exe)


def download_dsftool(tools_dir):
    """Fetch Laminar's xptools bundle for this OS and return the DSFTool path."""
    sysname = {"Linux": "lin", "Darwin": "mac", "Windows": "win"}[platform.system()]
    req = urllib.request.Request(XPTOOLS_PAGE, headers={"User-Agent": "HermosaXplane/1.0"})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    links = re.findall(r'https?://[^"\'\s>]*xptools_' + sysname + r'[^"\'\s>]*\.zip', html)
    if not links:
        raise RuntimeError(f"no xptools_{sysname} download link found on {XPTOOLS_PAGE}")
    url = links[0]
    os.makedirs(tools_dir, exist_ok=True)
    zpath = os.path.join(tools_dir, os.path.basename(url))
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "HermosaXplane/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(zpath, "wb") as f:
        shutil.copyfileobj(r, f)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tools_dir)
    path = find_dsftool(None, tools_dir)
    if not path:
        raise RuntimeError("DSFTool not found in downloaded xptools archive")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def compile_dsf(dsftool, txt_path, dsf_path):
    os.makedirs(os.path.dirname(dsf_path), exist_ok=True)
    r = subprocess.run([dsftool, "--text2dsf", txt_path, dsf_path], capture_output=True, text=True)
    print(r.stdout[-2000:])
    if r.returncode != 0 or not os.path.exists(dsf_path):
        print(r.stderr[-4000:])
        raise RuntimeError("DSFTool failed")
    # Round-trip check: decompile and make sure our objects/polygons survived.
    chk = dsf_path + ".check.txt"
    r2 = subprocess.run([dsftool, "--dsf2text", dsf_path, chk], capture_output=True, text=True)
    if r2.returncode == 0 and os.path.exists(chk):
        with open(chk, encoding="utf-8", errors="replace") as f:
            t = f.read()
        print(f"  DSF verified: {t.count(chr(10) + 'OBJECT ')} objects, "
              f"{t.count('BEGIN_POLYGON')} polygons, {t.count('BEGIN_SEGMENT')} road segments")
        os.remove(chk)
