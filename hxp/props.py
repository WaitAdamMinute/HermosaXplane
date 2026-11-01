"""Procedural 3D props: trees (palms, broadleaf, cypress) and street lights.

All props share one texture atlas (`props.dds`, alpha-tested) plus a night
texture (`props_LIT.dds`) so X-Plane can batch them efficiently.  Each model
variant is an OBJ8 file; the DSF places thousands of instances of them.
"""
import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw

from .buildings import Mesh, write_obj

ATLAS = 2048

# Atlas regions in pixels: name -> (x, y, w, h)
REGIONS = {
    "leaf0": (0, 0, 512, 512), "leaf1": (512, 0, 512, 512),
    "leaf2": (1024, 0, 512, 512), "leaf3": (1536, 0, 512, 512),
    "frond0": (0, 512, 1024, 256), "frond1": (1024, 512, 1024, 256),
    "frond_dead": (0, 768, 1024, 256), "conifer": (1024, 768, 512, 256),
    "palm_bark": (0, 1024, 128, 1024), "bark": (128, 1024, 128, 1024),
    "metal": (256, 1024, 128, 128), "lens": (384, 1024, 128, 128),
    "shrub": (512, 1024, 512, 512),
    "net": (1024, 1024, 512, 128), "tower_wall": (1024, 1152, 256, 256),
    "tower_white": (1280, 1152, 128, 128), "tower_ramp": (1408, 1152, 128, 128),
    "sign_red": (1536, 1024, 128, 64),
}


def uv_rect(name):
    x, y, w, h = REGIONS[name]
    i = 1.5
    return ((x + i) / ATLAS, 1 - (y + h - i) / ATLAS, (x + w - i) / ATLAS, 1 - (y + i) / ATLAS)  # u0, v0, u1, v1


# --------------------------------------------------------------------------
# Atlas painting


def _foliage(w, h, rng, base, light, density=1.0, conifer=False):
    S = 2
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy = w * S / 2, h * S / 2
    # silhouette = a cluster of overlapping lobes (cumulus-like crown)
    lobes = [(0.0, 0.0, 0.62)]
    for _ in range(rng.randint(6, 10)):
        a = rng.uniform(0, 2 * math.pi)
        dist = rng.uniform(0.25, 0.5)
        lobes.append((math.cos(a) * dist, math.sin(a) * dist, rng.uniform(0.3, 0.45)))
    n = int(11000 * density)
    for _ in range(n):
        lx, ly, lr = lobes[rng.randrange(len(lobes))]
        a = rng.uniform(0, 2 * math.pi)
        q = math.sqrt(rng.random())
        ux, uy = lx + math.cos(a) * q * lr, ly + math.sin(a) * q * lr
        rr = min(1.0, math.hypot(ux, uy))
        x = cx + ux * cx * (0.95 if conifer else 1.0)
        y = cy + uy * cy
        # light from upper-left, darker toward lower-right and the core
        shade = 0.55 + 0.45 * (1 - ((x - cx) / cx * 0.4 + (y - cy) / cy * 0.6 + 1) / 2) - 0.25 * (1 - rr)
        shade = max(0.25, min(1.15, shade + rng.uniform(-0.12, 0.12)))
        col = tuple(int(min(255, (base[i] + (light[i] - base[i]) * rng.random()) * shade)) for i in range(3))
        L = rng.uniform(5, 11) * S * (0.6 if conifer else 1.0)
        ang = rng.uniform(0, math.pi)
        dx, dy = math.cos(ang) * L / 2, math.sin(ang) * L / 2
        d.line([(x - dx, y - dy), (x + dx, y + dy)], fill=col + (255,), width=int(rng.uniform(2.5, 4.5) * S))
    im = im.resize((w, h), Image.LANCZOS)
    a = np.asarray(im).copy()
    a[..., 3] = np.where(a[..., 3] > 110, 255, 0)
    return Image.fromarray(a)


def _frond(w, h, rng, col, tip, dead=False):
    S = 2
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    W, H = w * S, h * S
    cy = H / 2
    for i in range(0, W, 3 * S):
        t = i / W
        half = H * 0.48 * math.sin(min(1.0, t * 1.15) * math.pi) ** 0.7 * (1 - 0.35 * t)
        c = tuple(int(col[k] + (tip[k] - col[k]) * t + rng.uniform(-14, 14)) for k in range(3))
        for side in (-1, 1):
            droop = half * rng.uniform(0.85, 1.0)
            ex = i + droop * (0.55 if not dead else 0.2)
            ey = cy + side * droop
            d.line([(i, cy), (ex, ey)], fill=c + (255,), width=int(2.2 * S))
    d.line([(0, cy), (W, cy)], fill=(120, 110, 70, 255) if not dead else (110, 90, 60, 255), width=int(3 * S))
    return im.resize((w, h), Image.LANCZOS)


def _bark(w, h, rng, palm):
    a = np.zeros((h, w, 4), np.uint8)
    base = np.array([125, 112, 92] if palm else [92, 78, 62], float)
    r = np.random.default_rng(rng.randrange(1 << 30))
    col = base + r.normal(0, 10, (h, w, 1))
    if palm:
        rows = (np.arange(h)[:, None] % 24 < 5)
        col = col * np.where(rows[..., None], 0.75, 1.0)
    else:
        streak = r.normal(0, 1, (1, w, 1)).repeat(h, 0)
        col = col + streak * 12
    a[..., :3] = np.clip(col, 0, 255)
    a[..., 3] = 255
    return Image.fromarray(a)


def paint_atlas(seed=11):
    rng = random.Random(seed)
    im = Image.new("RGBA", (ATLAS, ATLAS), (0, 0, 0, 0))
    lit = Image.new("RGB", (ATLAS, ATLAS), (0, 0, 0))
    greens = [((38, 62, 30), (112, 140, 72)), ((44, 70, 36), (120, 150, 80)),
              ((52, 66, 34), (138, 146, 82)), ((34, 56, 34), (96, 128, 70))]
    for i, (b, l) in enumerate(greens):
        x, y, w, h = REGIONS[f"leaf{i}"]
        im.paste(_foliage(w, h, rng, b, l, density=1.0 + 0.15 * i), (x, y))
    x, y, w, h = REGIONS["shrub"]
    im.paste(_foliage(w, h, rng, (46, 66, 36), (118, 136, 74), density=1.3), (x, y))
    x, y, w, h = REGIONS["conifer"]
    im.paste(_foliage(w, h, rng, (28, 48, 30), (70, 96, 58), density=1.6, conifer=True), (x, y))
    for name, c, t in (("frond0", (70, 104, 44), (128, 146, 72)), ("frond1", (82, 112, 52), (150, 156, 86))):
        x, y, w, h = REGIONS[name]
        im.paste(_frond(w, h, rng, c, t), (x, y))
    x, y, w, h = REGIONS["frond_dead"]
    im.paste(_frond(w, h, rng, (150, 120, 76), (176, 148, 98), dead=True), (x, y))
    for name, palm in (("palm_bark", True), ("bark", False)):
        x, y, w, h = REGIONS[name]
        im.paste(_bark(w, h, rng, palm), (x, y))
    x, y, w, h = REGIONS["metal"]
    im.paste(Image.new("RGBA", (w, h), (118, 122, 126, 255)), (x, y))
    # volleyball net: white top band, black mesh, transparent holes
    x, y, w, h = REGIONS["net"]
    net = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dn = ImageDraw.Draw(net)
    for k in range(0, w, 6):
        dn.line([(k, 0), (k, h)], fill=(20, 20, 20, 255), width=1)
    for k in range(0, h, 6):
        dn.line([(0, k), (w, k)], fill=(20, 20, 20, 255), width=1)
    dn.rectangle([0, 0, w, 10], fill=(245, 245, 245, 255))
    dn.rectangle([0, h - 5, w, h], fill=(245, 245, 245, 255))
    im.paste(net, (x, y))
    # LA County lifeguard tower: pale blue boards with a band of windows
    x, y, w, h = REGIONS["tower_wall"]
    tw = Image.new("RGBA", (w, h), (150, 196, 222, 255))
    dt = ImageDraw.Draw(tw)
    for k in range(0, h, 12):
        dt.line([(0, k), (w, k)], fill=(128, 172, 200, 255), width=2)
    dt.rectangle([10, 40, w - 10, 150], fill=(246, 246, 244, 255))
    dt.rectangle([16, 46, w - 16, 144], fill=(48, 62, 74, 255))
    for k in range(16, w - 16, 56):
        dt.line([(k, 46), (k, 144)], fill=(246, 246, 244, 255), width=6)
    im.paste(tw, (x, y))
    x, y, w, h = REGIONS["tower_white"]
    im.paste(Image.new("RGBA", (w, h), (244, 244, 240, 255)), (x, y))
    x, y, w, h = REGIONS["tower_ramp"]
    rp = Image.new("RGBA", (w, h), (176, 150, 116, 255))
    dr = ImageDraw.Draw(rp)
    for k in range(0, h, 10):
        dr.line([(0, k), (w, k)], fill=(140, 116, 88, 255), width=2)
    im.paste(rp, (x, y))
    x, y, w, h = REGIONS["sign_red"]
    im.paste(Image.new("RGBA", (w, h), (196, 40, 36, 255)), (x, y))
    x, y, w, h = REGIONS["lens"]
    im.paste(Image.new("RGBA", (w, h), (226, 222, 205, 255)), (x, y))
    lit.paste(Image.new("RGB", (w, h), (255, 196, 120)), (x, y))
    return im, lit


# --------------------------------------------------------------------------
# Geometry helpers


def _quad_uv(name, flip=False):
    u0, v0, u1, v1 = uv_rect(name)
    if flip:
        u0, u1 = u1, u0
    return [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]


def _cylinder(m, x, z, y0, y1, r0, r1, sides, uvname, lean=(0.0, 0.0)):
    u0, v0, u1, v1 = uv_rect(uvname)
    for k in range(sides):
        a0 = 2 * math.pi * k / sides
        a1 = 2 * math.pi * (k + 1) / sides
        p = []
        for a, r, yy, lx, lz in ((a0, r0, y0, 0, 0), (a1, r0, y0, 0, 0),
                                 (a1, r1, y1, lean[0], lean[1]), (a0, r1, y1, lean[0], lean[1])):
            p.append((x + lx + math.cos(a) * r, yy, z + lz + math.sin(a) * r))
        am = (a0 + a1) / 2
        n = (math.cos(am), 0.0, math.sin(am))
        su0 = u0 + (u1 - u0) * k / sides
        su1 = u0 + (u1 - u0) * (k + 1) / sides
        m.quad(p[0], p[1], p[2], p[3], n, [(su0, v0), (su1, v0), (su1, v1), (su0, v1)])


def _card(m, c, ax, ay, size_u, size_v, uvname, normal):
    """Quad centred at c spanned by unit axes ax (u) and ay (v)."""
    hu, hv = size_u / 2, size_v / 2
    p = [tuple(c[i] - ax[i] * hu - ay[i] * hv for i in range(3)),
         tuple(c[i] + ax[i] * hu - ay[i] * hv for i in range(3)),
         tuple(c[i] + ax[i] * hu + ay[i] * hv for i in range(3)),
         tuple(c[i] - ax[i] * hu + ay[i] * hv for i in range(3))]
    m.quad(p[0], p[1], p[2], p[3], normal, _quad_uv(uvname))


def _norm(v):
    L = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / L for x in v)


def broadleaf(height, crown_r, seed, leaf="leaf0"):
    rng = random.Random(seed)
    m = Mesh()
    trunk_top = height * rng.uniform(0.38, 0.5)
    _cylinder(m, 0, 0, -0.5, trunk_top + crown_r * 0.4, 0.12 + 0.012 * height, 0.08 + 0.008 * height, 6, "bark")
    cy = height - min(crown_r * 0.95, height * 0.42)
    ry = (height - cy)
    leaves = [leaf] + [f"leaf{rng.randrange(4)}"]
    # 3 crossed vertical cards through the crown
    for k in range(3):
        a = math.pi * k / 3 + rng.uniform(-0.2, 0.2)
        ax = (math.cos(a), 0.0, math.sin(a))
        _card(m, (0, cy, 0), ax, (0, 1, 0), crown_r * 2.1, ry * 2.05, leaves[k % 2], _norm((-ax[2], 0.3, ax[0])))
    # 2 horizontal layers
    for hy, s in ((cy - ry * 0.25, 1.9), (cy + ry * 0.45, 1.5)):
        a = rng.uniform(0, math.pi)
        _card(m, (0, hy, 0), (math.cos(a), 0, math.sin(a)), (-math.sin(a), 0, math.cos(a)),
              crown_r * s, crown_r * s, leaves[0], (0, 1, 0))
    # small cards scattered on the crown ellipsoid, normals pointing outward
    for _ in range(10):
        th = rng.uniform(0, 2 * math.pi)
        ph = rng.uniform(-0.3, 1.2)
        nx, ny, nz = math.cos(th) * math.cos(ph), math.sin(ph), math.sin(th) * math.cos(ph)
        c = (nx * crown_r * 0.75, cy + ny * ry * 0.75, nz * crown_r * 0.75)
        ax = _norm((-nz, 0, nx))
        ay = _norm((ny * nx * -1, math.cos(ph), ny * nz * -1))
        s = crown_r * rng.uniform(0.8, 1.1)
        _card(m, c, ax, ay, s, s, leaves[rng.randrange(2)], (nx, ny, nz))
    return m


def palm(height, seed, kind="fan"):
    rng = random.Random(seed)
    m = Mesh()
    lean = (rng.uniform(-0.6, 0.6), rng.uniform(-0.6, 0.6))
    if kind == "fan":   # Mexican fan palm: very thin, tall
        r0, r1, fl, nf = 0.24, 0.17, 2.6, 18
    else:               # Canary Island date palm: thick trunk, big crown
        r0, r1, fl, nf = 0.42, 0.36, 4.6, 26
    _cylinder(m, 0, 0, -0.5, height, r0, r1, 8, "palm_bark", lean=lean)
    tx, ty, tz = lean[0], height, lean[1]
    # skirt of dead fronds hanging under the crown (fan palms keep a "skirt")
    for k in range(10 if kind == "fan" else 6):
        a = 2 * math.pi * k / (10 if kind == "fan" else 6) + rng.uniform(-0.2, 0.2)
        d = (math.cos(a), 0, math.sin(a))
        c = (tx + d[0] * 0.35, ty - fl * 0.42, tz + d[2] * 0.35)
        _card(m, c, (0, -1, 0), (-d[2], 0, d[0]), fl * 0.85, fl * 0.35, "frond_dead", d)
    # live fronds: 3-segment drooping strips radiating from the crown
    for k in range(nf):
        a = 2 * math.pi * k / nf + rng.uniform(-0.15, 0.15)
        up = rng.uniform(-0.15, 0.75)
        dirx, dirz = math.cos(a), math.sin(a)
        side = (-dirz, 0, dirx)
        L = fl * rng.uniform(0.8, 1.1)
        w = L * 0.36
        pts = [(tx, ty + 0.1, tz)]
        elev = up
        for s in range(3):
            seg = L / 3
            px, py, pz = pts[-1]
            pts.append((px + dirx * seg * math.cos(elev), py + seg * math.sin(elev), pz + dirz * seg * math.cos(elev)))
            elev -= rng.uniform(0.35, 0.6)
        name = "frond0" if k % 2 else "frond1"
        u0, v0, u1, v1 = uv_rect(name)
        for s in range(3):
            a0, a1 = pts[s], pts[s + 1]
            hw0 = w / 2 * (0.6 if s == 0 else 1.0 - 0.25 * (s - 1))
            hw1 = w / 2 * (1.0 - 0.3 * s)
            p0 = (a0[0] - side[0] * hw0, a0[1], a0[2] - side[2] * hw0)
            p1 = (a1[0] - side[0] * hw1, a1[1], a1[2] - side[2] * hw1)
            p2 = (a1[0] + side[0] * hw1, a1[1], a1[2] + side[2] * hw1)
            p3 = (a0[0] + side[0] * hw0, a0[1], a0[2] + side[2] * hw0)
            su0 = u0 + (u1 - u0) * s / 3
            su1 = u0 + (u1 - u0) * (s + 1) / 3
            n = _norm((dirx * 0.3, 1.0, dirz * 0.3))
            m.quad(p0, p1, p2, p3, n, [(su0, v0), (su1, v0), (su1, v1), (su0, v1)])
    return m


def cypress(height, seed):
    rng = random.Random(seed)
    m = Mesh()
    _cylinder(m, 0, 0, -0.5, 1.2, 0.12, 0.1, 5, "bark")
    r = max(0.6, height * 0.11)
    for k in range(3):
        a = math.pi * k / 3 + rng.uniform(-0.2, 0.2)
        ax = (math.cos(a), 0.0, math.sin(a))
        _card(m, (0, height * 0.52, 0), ax, (0, 1, 0), r * 2.2, height * 1.0, "conifer", _norm((-ax[2], 0.2, ax[0])))
    return m


def shrub(size, seed):
    rng = random.Random(seed)
    m = Mesh()
    for k in range(3):
        a = math.pi * k / 3 + rng.uniform(-0.3, 0.3)
        ax = (math.cos(a), 0.0, math.sin(a))
        _card(m, (0, size * 0.42, 0), ax, (0, 1, 0), size * 1.6, size * 0.95, "shrub", _norm((-ax[2], 0.4, ax[0])))
    _card(m, (0, size * 0.75, 0), (1, 0, 0), (0, 0, 1), size * 1.4, size * 1.4, "shrub", (0, 1, 0))
    return m


def street_light(kind="cobra", base=0.0):
    """Pole + arm + glowing head. Origin at pole base, arm points +X (east)."""
    m = Mesh()
    if kind == "cobra":
        h, arm = 8.5, 2.4
        _cylinder(m, 0, 0, -0.3, h, 0.11, 0.07, 6, "metal")
        # arm (box)
        y = h - 0.15
        _box(m, (0.0, y - 0.07, -0.05), (arm, y + 0.07, 0.05), "metal")
        _box(m, (arm - 0.2, y - 0.22, -0.22), (arm + 0.55, y + 0.05, 0.22), "metal")
        # glass bowl under the head (lit); its sides make the lamp read as a
        # bright point from any angle at night
        _box(m, (arm - 0.12, y - 0.34, -0.19), (arm + 0.5, y - 0.21, 0.19), "lens")
    else:  # decorative acorn post lamp (pier / downtown)
        h = 4.2
        _cylinder(m, 0, 0, -0.3, h, 0.09, 0.06, 6, "metal")
        _cylinder(m, 0, 0, h, h + 0.7, 0.2, 0.24, 8, "lens")
        _cylinder(m, 0, 0, h + 0.7, h + 0.85, 0.26, 0.05, 8, "metal")
    if base:
        m.v = [(x, y + base, z, a, b, c, s, t) for x, y, z, a, b, c, s, t in m.v]
    return m


def _box(m, a, b, uvname):
    (x0, y0, z0), (x1, y1, z1) = a, b
    u = _quad_uv(uvname)
    m.quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1), u)
    m.quad((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (0, 0, -1), u)
    m.quad((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (-1, 0, 0), u)
    m.quad((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (1, 0, 0), u)
    m.quad((x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0), (0, 1, 0), u)
    m.quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0), u)


def lifeguard_tower():
    """LA County style tower: cabin on stilts facing +X (the water), ramp at the back."""
    m = Mesh()
    deck, cab_h, w, d = 1.6, 2.3, 2.6, 2.4
    for sx in (-1, 1):
        for sz in (-1, 1):
            x, z = sx * (d / 2 - 0.15), sz * (w / 2 - 0.15)
            _box(m, (x - 0.09, -0.6, z - 0.09), (x + 0.09, deck, z + 0.09), "tower_white")
    _box(m, (-d / 2 - 0.5, deck - 0.15, -w / 2 - 0.2), (d / 2 + 0.6, deck, w / 2 + 0.2), "tower_white")  # deck
    # cabin walls (window band texture) and roof
    x0, x1, z0, z1, y0, y1 = -d / 2, d / 2, -w / 2, w / 2, deck, deck + cab_h
    uw = _quad_uv("tower_wall")
    m.quad((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (1, 0, 0), uw)      # front (sea)
    m.quad((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (-1, 0, 0), uw)     # back
    m.quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1), uw)      # sides
    m.quad((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (0, 0, -1), uw)
    _box(m, (x0 - 0.35, y1, z0 - 0.35), (x1 + 0.35, y1 + 0.18, z1 + 0.35), "tower_white")   # roof
    _box(m, (x1 - 0.05, y1 + 0.18, -0.5), (x1 + 0.05, y1 + 0.55, 0.5), "sign_red")          # sign
    # front railing posts and rail
    _box(m, (x1 + 0.5, deck, z0 - 0.15), (x1 + 0.56, deck + 0.95, z1 + 0.15), "tower_white")
    # ramp down to the sand behind the tower
    ur = _quad_uv("tower_ramp")
    m.quad((x0 - 4.2, 0.0, 0.5), (x0 - 4.2, 0.0, -0.5), (x0 - 0.5, deck, -0.5), (x0 - 0.5, deck, 0.5),
           (-0.4, 0.9, 0), ur)
    return m


def volleyball_net(width):
    m = Mesh()
    hw = width / 2
    for x in (-hw, hw):
        _cylinder(m, x, 0, -0.6, 2.55, 0.05, 0.05, 6, "tower_white")
    u = _quad_uv("net")
    m.quad((-hw, 1.43, 0), (hw, 1.43, 0), (hw, 2.43, 0), (-hw, 2.43, 0), (0, 0, 1), u)
    return m


# --------------------------------------------------------------------------
# Library of variants

BROADLEAF_HEIGHTS = [3.5, 5, 6.5, 8, 10, 12, 14.5, 17, 20, 24, 28]
PALM_HEIGHTS = [6, 9, 12, 15, 18, 21, 24, 27, 30]


class PropLibrary:
    """Writes all prop OBJ variants and hands out (obj_path, heading) for placements."""

    def __init__(self, pack, tex_rel_from_obj, ext, lod_far=5000.0):
        self.pack = pack
        self.dir = os.path.join(pack, "objects", "props")
        os.makedirs(self.dir, exist_ok=True)
        self.tex = f"{tex_rel_from_obj}/props.{ext}"
        self.lit = f"{tex_rel_from_obj}/props_LIT.{ext}"
        self.lod_far = lod_far
        self.written = {}

    def _write(self, name, mesh, lit=False, lod=None):
        if name not in self.written:
            write_obj(os.path.join(self.dir, name + ".obj"), mesh, self.tex, self.lit if lit else None,
                      lod or self.lod_far)
            self.written[name] = f"objects/props/{name}.obj"
        return self.written[name]

    def broadleaf(self, height, crown_r, rnd):
        h = min(BROADLEAF_HEIGHTS, key=lambda v: abs(v - height))
        rbin = 1 if crown_r < h * 0.28 else (2 if crown_r < h * 0.42 else 3)
        var = rnd.randrange(3)
        cr = h * (0.22, 0.34, 0.48)[rbin - 1]
        name = f"tree_broad_{h:g}_{rbin}_{var}"
        if name not in self.written:
            self._write(name, broadleaf(h, cr, hash(name) & 0xFFFF, leaf=f"leaf{var}"))
        return self.written[name]

    def palm(self, height, rnd, kind=None):
        h = min(PALM_HEIGHTS, key=lambda v: abs(v - height))
        kind = kind or ("fan" if h >= 12 and rnd.random() < 0.75 else "date")
        var = rnd.randrange(2)
        name = f"tree_palm_{kind}_{h:g}_{var}"
        if name not in self.written:
            self._write(name, palm(h, hash(name) & 0xFFFF, kind))
        return self.written[name]

    def cypress(self, height, rnd):
        h = min([6, 9, 12, 15], key=lambda v: abs(v - height))
        name = f"tree_cypress_{h:g}"
        if name not in self.written:
            self._write(name, cypress(h, 3))
        return self.written[name]

    def shrub(self, size, rnd):
        s = min([1.2, 2.0, 3.0], key=lambda v: abs(v - size))
        name = f"shrub_{s:g}_{rnd.randrange(2)}"
        if name not in self.written:
            self._write(name, shrub(s, hash(name) & 0xFFFF), lod=2500)
        return self.written[name]

    def lifeguard_tower(self):
        if "lifeguard_tower" not in self.written:
            self._write("lifeguard_tower", lifeguard_tower(), lod=4000)
        return self.written["lifeguard_tower"]

    def volleyball_net(self, width):
        w = round(width * 2) / 2
        name = f"volleyball_net_{w:g}"
        if name not in self.written:
            self._write(name, volleyball_net(w), lod=2500)
        return self.written[name]

    def street_light(self, kind, base=0.0):
        name = f"streetlight_{kind}" + (f"_b{base:g}" if base else "")
        if name not in self.written:
            self._write(name, street_light(kind, base), lit=True, lod=4000)
        return self.written[name]
