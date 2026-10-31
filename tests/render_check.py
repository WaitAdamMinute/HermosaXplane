"""Software-render an oblique view of a built pack (sanity check without X-Plane).

    python tests/render_check.py build/zzz_Hermosa_Beach_Photoreal build/+33-119.txt out.jpg [lon lat]
Parses the DSF text + OBJ8 files, drapes the ortho textures and draws every
triangle with its texture colour (painter's algorithm).
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hxp.geo import LocalFrame  # noqa: E402

_tex = {}


def tex(path):
    if path not in _tex:
        im = Image.open(path).convert("RGBA")
        if path.endswith(".dds"):
            im = im.transpose(Image.FLIP_TOP_BOTTOM)  # stored bottom-up for X-Plane
        _tex[path] = np.asarray(im)
    return _tex[path]


def sample(arr, s, t):
    h, w = arr.shape[:2]
    x = min(w - 1, max(0, int(s * w)))
    y = min(h - 1, max(0, int((1 - t) * h)))
    return arr[y, x]


def parse_obj(path):
    tx, vt, idx, ntris = None, [], [], None
    for ln in open(path):
        p = ln.split()
        if not p:
            continue
        if p[0] == "TEXTURE":
            tx = os.path.normpath(os.path.join(os.path.dirname(path), p[1]))
        elif p[0] == "VT":
            vt.append([float(v) for v in p[1:9]])
        elif p[0] in ("IDX", "IDX10"):
            idx += [int(v) for v in p[1:]]
        elif p[0] == "POINT_COUNTS":
            counts = (int(p[1]), int(p[4]))
        elif p[0] == "TRIS":
            ntris = (int(p[1]), int(p[2]))
    assert len(vt) == counts[0] and len(idx) == counts[1], f"count mismatch {path}"
    assert ntris and ntris[0] + ntris[1] <= len(idx) and max(idx) < len(vt), f"bad TRIS/IDX {path}"
    return tx, np.array(vt), idx


def main(pack, dsf_txt, out, lon=None, lat=None):
    objs, pols, placements, polys = [], [], [], []
    cur = None
    for ln in open(dsf_txt):
        p = ln.split()
        if not p:
            continue
        if p[0] == "OBJECT_DEF":
            objs.append(p[1])
        elif p[0] == "POLYGON_DEF":
            pols.append(p[1])
        elif p[0] == "OBJECT":
            placements.append((int(p[1]), float(p[2]), float(p[3])))
        elif p[0] == "BEGIN_POLYGON":
            cur = (int(p[1]), [])
        elif p[0] == "POLYGON_POINT":
            cur[1].append(tuple(float(v) for v in p[1:5]))
        elif p[0] == "END_POLYGON":
            polys.append(cur)
    print(f"{len(objs)} obj defs, {len(placements)} placements, {len(polys)} polygons")
    if lon is None:
        lon = np.mean([q[1] for q in placements])
        lat = np.mean([q[2] for q in placements])
    fr = LocalFrame(lon, lat)
    W, H = 1600, 1000
    cam = np.array([-260.0, 230.0, 330.0])   # east, up, south(+) -> looking NE from SW over the target
    tgt = np.array([0.0, 0.0, 0.0])
    fwd = tgt - cam; fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 1, 0]); right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    f = W / 2 / math.tan(math.radians(30))

    def proj(P):
        d = P - cam
        z = d @ fwd
        return W / 2 + (d @ right) * f / z, H / 2 - (d @ up) * f / z, z

    tris = []
    # ground: rasterise draped polygon texture as a grid of quads around the target
    for pidx, pts in polys:
        pol = os.path.join(pack, pols[pidx])
        tpath = None
        for ln in open(pol):
            if ln.startswith("TEXTURE_NOWRAP"):
                tpath = os.path.normpath(os.path.join(os.path.dirname(pol), ln.split()[1]))
        arr = tex(tpath)
        # bilinear UV from the polygon's bounding samples
        lons = [q[0] for q in pts]; lats = [q[1] for q in pts]
        A = np.array([[q[0], q[1], 1] for q in pts]); su = np.linalg.lstsq(A, [q[2] for q in pts], rcond=None)[0]
        tv = np.linalg.lstsq(A, [q[3] for q in pts], rcond=None)[0]
        from shapely.geometry import Point, Polygon
        poly = Polygon([(q[0], q[1]) for q in pts])
        step = 8.0
        for gx in np.arange(-700, 700, step):
            for gz in np.arange(-900, 400, step):
                lo, la = fr.to_ll(gx, -gz)
                if not (min(lons) <= lo <= max(lons) and min(lats) <= la <= max(lats)) or not poly.contains(Point(lo, la)):
                    continue
                c = sample(arr, su @ [lo, la, 1], tv @ [lo, la, 1])
                if c[3] < 128:
                    continue
                q = [np.array([gx + dx, 0, gz + dz]) for dx, dz in ((0, 0), (step, 0), (step, step), (0, step))]
                tris.append((q, tuple(int(v) for v in c[:3])))
    for oi, lo, la in placements:
        x, y = fr.to_xy(lo, la)
        if abs(x) > 700 or y < -400 or y > 900:
            continue
        tx, vt, idx = parse_obj(os.path.join(pack, objs[oi]))
        arr = tex(tx)
        for k in range(0, len(idx), 3):
            vs = vt[idx[k:k + 3]]
            P = [np.array([v[0] + x, v[1], v[2] - y]) for v in vs]
            s, t = vs[:, 6].mean(), vs[:, 7].mean()
            c = sample(arr, s, t)[:3].astype(float)
            n = vs[0, 3:6]
            c *= 0.65 + 0.35 * max(0.0, n @ np.array([0.4, 0.8, 0.45]))
            tris.append((P, tuple(int(v) for v in c)))
    im = Image.new("RGB", (W, H), (150, 190, 230))
    d = ImageDraw.Draw(im)
    drawn = []
    for P, c in tris:
        pr = [proj(p) for p in P]
        if min(q[2] for q in pr) < 1:
            continue
        drawn.append((-np.mean([q[2] for q in pr]), [(q[0], q[1]) for q in pr], c))
    drawn.sort(key=lambda r: r[0])
    for _, pts, c in drawn:
        d.polygon(pts, fill=c)
    im.save(out, quality=90)
    print("wrote", out)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[1], a[2], *(float(v) for v in a[3:5]))
