"""Render a sim-style view of a built pack (approximation of the in-sim look).

A small software rasteriser: z-buffered, perspective-correct, mip-mapped
texturing of the real pack contents (draped photo polygons + every building /
pier OBJ8 at its DSF placement), with sun lighting, sky and distance haze.
Terrain is assumed flat (the pack has no elevation data; X-Plane supplies it).

    python tests/render_view.py <pack_dir> <dsf.txt> <out.jpg> <preset>
presets: pier, downtown, approach
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hxp.geo import LocalFrame  # noqa: E402

PRESETS = {
    # target lon/lat, camera offset from target (east, up, north) metres, fov
    "pier": dict(target=(-118.3995, 33.8618), cam=(-620.0, 210.0, -340.0), fov=62),
    "downtown": dict(target=(-118.3975, 33.8625), cam=(-330.0, 120.0, 260.0), fov=60),
    "approach": dict(target=(-118.3940, 33.8620), cam=(-1500.0, 520.0, -900.0), fov=55),
    "street": dict(target=(-118.3955, 33.8622), cam=(-120.0, 28.0, -95.0), fov=65),
    "night": dict(target=(-118.3975, 33.8625), cam=(-330.0, 120.0, 260.0), fov=60, night=True),
}

W, H = 1920, 1080
SS = 1  # supersampling factor


class Tex:
    def __init__(self, path):
        im = Image.open(path).convert("RGBA")
        if path.endswith(".dds"):
            im = im.transpose(Image.FLIP_TOP_BOTTOM)
        self.levels = []
        cur = im
        while True:
            self.levels.append(np.asarray(cur).astype(np.float32) / 255.0)
            if min(cur.size) <= 8:
                break
            cur = cur.resize((cur.size[0] // 2, cur.size[1] // 2), Image.BOX)
        self.size = im.size[0]

    def sample(self, u, v, lod):
        lv = self.levels[min(len(self.levels) - 1, max(0, int(lod)))]
        h, w = lv.shape[:2]
        x = np.clip((u * w).astype(np.int32), 0, w - 1)
        y = np.clip(((1.0 - v) * h).astype(np.int32), 0, h - 1)
        return lv[y, x]


_tex = {}


def tex(path):
    if path not in _tex:
        _tex[path] = Tex(path)
    return _tex[path]


def parse_obj(path):
    tx, lit, vt, idx = None, None, [], []
    with open(path) as f:
        for ln in f:
            p = ln.split()
            if not p:
                continue
            if p[0] == "TEXTURE":
                tx = os.path.normpath(os.path.join(os.path.dirname(path), p[1]))
            elif p[0] == "TEXTURE_LIT":
                lit = os.path.normpath(os.path.join(os.path.dirname(path), p[1]))
            elif p[0] == "VT":
                vt.append([float(v) for v in p[1:9]])
            elif p[0] in ("IDX", "IDX10"):
                idx += [int(v) for v in p[1:]]
    return tx, lit, np.array(vt, dtype=np.float64), np.array(idx, dtype=np.int64)


class Renderer:
    def __init__(self, cam, target, fov, night=False):
        self.night = night
        self.w, self.h = W * SS, H * SS
        self.cam = np.array(cam, float)
        fwd = np.array(target, float) - self.cam
        self.fwd = fwd / np.linalg.norm(fwd)
        r = np.cross(self.fwd, [0, 1, 0])
        self.right = r / np.linalg.norm(r)
        self.up = np.cross(self.right, self.fwd)
        self.f = self.w / 2 / math.tan(math.radians(fov / 2))
        self.color = np.zeros((self.h, self.w, 3), np.float32)
        self.depth = np.full((self.h, self.w), np.inf, np.float32)
        self.sun = np.array([-0.45, 0.62, 0.64])  # from the south-west, afternoon (east, up, south)
        self.sun /= np.linalg.norm(self.sun)

    def project(self, P):
        d = P - self.cam
        z = d @ self.fwd
        x = self.w / 2 + (d @ self.right) * self.f / np.maximum(z, 1e-3)
        y = self.h / 2 - (d @ self.up) * self.f / np.maximum(z, 1e-3)
        return x, y, z

    def draw(self, P, UV, normals, texture, alpha_test=True, shade=True, lit=None):
        """P: (n,3,3) world tris; UV: (n,3,2); normals: (n,3)."""
        x, y, z = self.project(P.reshape(-1, 3))
        x, y, z = x.reshape(-1, 3), y.reshape(-1, 3), z.reshape(-1, 3)
        keep = (z.min(1) > 2.0) & (x.max(1) >= 0) & (x.min(1) < self.w) & (y.max(1) >= 0) & (y.min(1) < self.h)
        if shade:
            ndl = np.clip(normals @ self.sun, 0, 1)
            light = 0.50 + 0.62 * ndl
        else:
            light = np.ones(len(P))
        if self.night:
            light = light * 0.07
        for k in np.nonzero(keep)[0]:
            self._tri(x[k], y[k], z[k], UV[k], texture, light[k], alpha_test, lit)

    def _tri(self, xs, ys, zs, uv, texture, light, alpha_test, lit=None):
        x0, x1 = max(int(xs.min()), 0), min(int(xs.max()) + 1, self.w)
        y0, y1 = max(int(ys.min()), 0), min(int(ys.max()) + 1, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (xs[2] - xs[0]) * (ys[1] - ys[0])
        if abs(area) < 1e-9:
            return
        gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (xs[2] - gx) * (ys[1] - gy)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (xs[0] - gx) * (ys[2] - gy)) / area
        w2 = 1 - w0 - w1
        m = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not m.any():
            return
        iz = w0 / zs[0] + w1 / zs[1] + w2 / zs[2]
        zz = 1.0 / iz
        sub = self.depth[y0:y1, x0:x1]
        m &= zz < sub
        if not m.any():
            return
        u = (w0 * uv[0, 0] / zs[0] + w1 * uv[1, 0] / zs[1] + w2 * uv[2, 0] / zs[2]) * zz
        v = (w0 * uv[0, 1] / zs[0] + w1 * uv[1, 1] / zs[1] + w2 * uv[2, 1] / zs[2]) * zz
        # mip level from texel / pixel area ratio
        uv_area = abs((uv[1, 0] - uv[0, 0]) * (uv[2, 1] - uv[0, 1]) - (uv[2, 0] - uv[0, 0]) * (uv[1, 1] - uv[0, 1]))
        texels = uv_area * texture.size ** 2
        lod = 0.5 * math.log2(max(texels / max(abs(area), 1e-6), 1.0))
        c = texture.sample(u[m], v[m], lod)
        if alpha_test:
            a = c[:, 3] > 0.5
        else:
            a = np.ones(len(c), bool)
        yy, xx = np.nonzero(m)
        yy, xx = yy[a], xx[a]
        col = c[a, :3] * light
        if self.night and lit is not None:
            col = col + lit.sample(u[m], v[m], lod)[a, :3] * 1.4
        self.color[y0 + yy, x0 + xx] = col
        self.depth[y0 + yy, x0 + xx] = zz[m][a]

    def finish(self, path):
        # sky gradient
        hh = np.linspace(0, 1, self.h)[:, None]
        sky_top, sky_hor = np.array([0.36, 0.55, 0.82]), np.array([0.80, 0.86, 0.92])
        if self.night:
            sky_top, sky_hor = np.array([0.01, 0.015, 0.04]), np.array([0.05, 0.06, 0.10])
        sky = sky_top * (1 - hh[..., None]) + sky_hor * hh[..., None]
        sky = np.broadcast_to(sky, (self.h, self.w, 3))
        img = np.where(np.isinf(self.depth)[..., None], sky, self.color)
        # aerial perspective haze
        d = np.where(np.isinf(self.depth), 0, self.depth)
        fog = (1 - np.exp(-d / 9000.0))[..., None] * 0.85
        img = img * (1 - fog) + sky_hor * fog
        if self.night:  # bloom around light sources
            glow = np.clip(img - 0.35, 0, None).astype(np.float32)
            g = Image.fromarray((np.clip(glow, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(9))
            img = img + np.asarray(g).astype(np.float32) / 255 * 1.2
        # mild filmic tone curve
        img = np.clip(img, 0, 1) ** 0.95
        out = Image.fromarray((img * 255).astype(np.uint8))
        if SS > 1:
            out = out.resize((W, H), Image.LANCZOS)
        out = out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=40, threshold=2))
        out.save(path, quality=92)


def load_dsf(path):
    objs, pols, placements, polys = [], [], [], []
    cur = None
    with open(path) as f:
        for ln in f:
            p = ln.split()
            if not p:
                continue
            if p[0] == "OBJECT_DEF":
                objs.append(p[1])
            elif p[0] == "POLYGON_DEF":
                pols.append(p[1])
            elif p[0] == "OBJECT":
                placements.append((int(p[1]), float(p[2]), float(p[3]), float(p[4]) if len(p) > 4 else 0.0))
            elif p[0] == "BEGIN_POLYGON":
                cur = (int(p[1]), [])
            elif p[0] == "BEGIN_WINDING":
                cur[1].append([])
            elif p[0] == "POLYGON_POINT":
                cur[1][-1].append(tuple(float(v) for v in p[1:5]))
            elif p[0] == "END_POLYGON":
                polys.append(cur)
    return objs, pols, placements, polys


def ground(r, pack, pols, polys, fr, radius):
    """Triangulate the draped photo polygons into a fine grid (flat terrain)."""
    import mapbox_earcut as earcut
    from shapely.geometry import Polygon, box

    for pidx, windings in polys:
        pol = os.path.join(pack, pols[pidx])
        with open(pol) as f:
            tpath = [ln.split()[1] for ln in f if ln.startswith("TEXTURE_NOWRAP")][0]
        t = tex(os.path.normpath(os.path.join(os.path.dirname(pol), tpath)))
        pts = [[fr.to_xy(q[0], q[1]) for q in w] for w in windings]
        uvs = np.array([q[2:4] for w in windings for q in w])
        xy = np.array([p for w in pts for p in w])
        A = np.c_[xy, np.ones(len(xy))]
        cu = np.linalg.lstsq(A, uvs[:, 0], rcond=None)[0]
        cv = np.linalg.lstsq(A, uvs[:, 1], rcond=None)[0]
        poly = Polygon(pts[0], pts[1:])
        cx, cz = r.cam[0], -r.cam[2]
        step = 40.0
        for gx in np.arange(cx - radius, cx + radius, step):
            for gy in np.arange(cz - radius, cz + radius, step):
                cell = box(gx, gy, gx + step, gy + step).intersection(poly)
                if cell.is_empty:
                    continue
                for c in [cell] if cell.geom_type == "Polygon" else [g for g in getattr(cell, "geoms", []) if g.geom_type == "Polygon"]:
                    ring = list(c.exterior.coords)[:-1]
                    holes = [list(h.coords)[:-1] for h in c.interiors]
                    allp = ring + [p for h in holes for p in h]
                    ends = np.cumsum([len(ring)] + [len(h) for h in holes]).astype(np.uint32)
                    ix = earcut.triangulate_float64(np.array(allp), ends)
                    if len(ix) == 0:
                        continue
                    P3 = np.array([[p[0], 0.0, -p[1]] for p in allp])[ix].reshape(-1, 3, 3)
                    UV = np.array([[cu @ [p[0], p[1], 1], cv @ [p[0], p[1], 1]] for p in allp])[ix].reshape(-1, 3, 2)
                    r.draw(P3, UV, np.tile([0, 1, 0], (len(P3), 1)), t, alpha_test=False)


def main(pack, dsf_txt, out, preset):
    pr = PRESETS[preset]
    objs, pols, placements, polys = load_dsf(dsf_txt)
    fr = LocalFrame(*pr["target"])
    ce, cu, cn = pr["cam"]
    r = Renderer((ce, cu, -cn), (0.0, 0.0, 0.0), pr["fov"], night=pr.get("night", False))
    radius = 4500.0
    # water below everything outside the photo
    sea = np.array([[[-2e4, -0.3, -2e4], [2e4, -0.3, -2e4], [2e4, -0.3, 2e4]],
                    [[-2e4, -0.3, -2e4], [2e4, -0.3, 2e4], [-2e4, -0.3, 2e4]]])
    water = Tex.__new__(Tex)
    water.levels = [np.array([[[0.13, 0.30, 0.42, 1.0]]], np.float32)]
    water.size = 1
    r.draw(sea, np.zeros((2, 3, 2)), np.tile([0, 1, 0], (2, 1)), water, alpha_test=False, shade=False)
    ground(r, pack, pols, polys, fr, radius)
    n = 0
    cache = {}
    for oi, lo, la, hd in placements:
        x, y = fr.to_xy(lo, la)
        if math.hypot(x - ce, y - cn) > radius:
            continue
        if oi not in cache:
            cache[oi] = parse_obj(os.path.join(pack, objs[oi]))
        tx, ltx, vt, idx = cache[oi]
        if len(idx) == 0:
            continue
        P = vt[idx][:, :3].copy()
        N = vt[idx][::3, 3:6].copy()
        if hd:
            c, s_ = math.cos(math.radians(hd)), math.sin(math.radians(hd))
            P[:, 0], P[:, 2] = P[:, 0] * c - P[:, 2] * s_, P[:, 0] * s_ + P[:, 2] * c
            N[:, 0], N[:, 2] = N[:, 0] * c - N[:, 2] * s_, N[:, 0] * s_ + N[:, 2] * c
        P[:, 0] += x
        P[:, 2] -= y
        UV = vt[idx][:, 6:8]
        r.draw(P.reshape(-1, 3, 3), UV.reshape(-1, 3, 2), N, tex(tx), lit=tex(ltx) if (ltx and r.night) else None)
        n += 1
    r.finish(out)
    print(f"{preset}: rendered {n} objects -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
