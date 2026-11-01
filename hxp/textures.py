"""Texture generation: DDS writer, building wall atlas, night-lighting texture."""
import io
import random
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# --------------------------------------------------------------------------
# DDS (DXT1 / DXT5) with a full mipmap chain


def _dxt_payload(img, fmt):
    b = io.BytesIO()
    img.save(b, "DDS", pixel_format=fmt)
    return b.getvalue()[128:]


def save_dds(img, path, alpha=False, flip=False):
    fmt = "DXT5" if alpha else "DXT1"
    img = img.convert("RGBA" if alpha else "RGB")
    if flip:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    w, h = img.size
    levels = []
    cur = img
    while True:
        levels.append(_dxt_payload(cur, fmt))
        if cur.size[0] <= 4 or cur.size[1] <= 4:
            break
        cur = cur.resize((max(4, cur.size[0] // 2), max(4, cur.size[1] // 2)), Image.BOX)
    linear = len(levels[0])
    hdr = struct.pack(
        "<4s7I44x", b"DDS ", 124,
        0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000,  # caps|h|w|pixfmt|mipcount|linearsize
        h, w, linear, 0, len(levels),
    )
    pf = struct.pack("<2I4s5I", 32, 0x4, fmt.encode(), 0, 0, 0, 0, 0)
    caps = struct.pack("<4I4x", 0x1000 | 0x8 | 0x400000, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(hdr + pf + caps)
        for lv in levels:
            f.write(lv)


def save_texture(img, path_noext, fmt, alpha=False, flip=False):
    if fmt == "dds":
        p = path_noext + ".dds"
        save_dds(img, p, alpha=alpha, flip=flip)
    else:
        p = path_noext + ".png"
        img.convert("RGBA" if alpha else "RGB").save(p, optimize=False)
    return p


# --------------------------------------------------------------------------
# Wall atlas

RESIDENTIAL = [
    (238, 236, 230), (233, 223, 198), (216, 200, 172), (204, 203, 198),
    (192, 207, 216), (224, 209, 182), (246, 243, 236), (192, 199, 178),
    (214, 176, 146), (172, 182, 192), (226, 226, 220), (205, 190, 165),
]
STORE_ACCENT = [(40, 70, 110), (120, 40, 40), (40, 90, 60), (60, 60, 60)]
GLASS = [(70, 95, 115), (85, 105, 100)]


class Swatch:
    def __init__(self, idx, layout):
        self.idx = idx
        cols = layout.tex // layout.sw
        self.x = (idx % cols) * layout.sw
        self.y = layout.tex - layout.strip + (idx // cols) * layout.sw
        t, sw, inset = layout.tex, layout.sw, 1.5
        self.u0 = (self.x + inset) / t
        self.u1 = (self.x + sw - inset) / t
        self.v1 = 1.0 - (self.y + inset) / t          # top of swatch
        self.v0 = 1.0 - (self.y + sw - inset) / t     # bottom of swatch


SIDING = [(214, 220, 222), (196, 208, 200), (232, 228, 214), (120, 138, 150)]
MODERN = [(242, 242, 240), (88, 92, 96), (226, 222, 214)]
BRICK = [(150, 78, 60), (172, 120, 92)]


class WallAtlas:
    """Fixed layout of wall swatches occupying the bottom strip of every texture.

    Each swatch = one bay (bay_width m) x one storey.  A style is a dict with
    ground / upper / blank / garage swatches.  `railing` is an alpha-tested
    swatch used for balconies and roof decks.
    """

    def __init__(self, tex, strip, seed=7):
        self.tex, self.strip = tex, strip
        self.sw = strip // 4
        self.capacity = (tex // self.sw) * 4
        self.rng = random.Random(seed)
        self.styles = {}
        self._draw = []   # (swatch, kind, colour, accent)
        n = 0

        def add(kind, col, acc=None):
            nonlocal n
            s = Swatch(n, self)
            self._draw.append((s, kind, col, acc))
            n += 1
            return s

        for k, c in enumerate(RESIDENTIAL):
            self.styles[f"res{k}"] = dict(ground=add("door", c), upper=add("window", c), blank=add("blank", c),
                                          garage=add("garagedoor", c))
        for k, c in enumerate(SIDING):
            self.styles[f"siding{k}"] = dict(ground=add("siding_door", c), upper=add("siding_window", c),
                                             blank=add("siding", c), garage=add("siding_garage", c))
        for k, c in enumerate(MODERN):
            self.styles[f"modern{k}"] = dict(ground=add("modern_ground", c), upper=add("modern_window", c),
                                             blank=add("blank", c), garage=add("modern_garage", c))
        for k, a in enumerate(STORE_ACCENT):
            c = RESIDENTIAL[(k * 5) % len(RESIDENTIAL)]
            self.styles[f"shop{k}"] = dict(ground=add("store", c, a), upper=add("window", c), blank=add("blank", c))
        for k, c in enumerate(BRICK):
            self.styles[f"brick{k}"] = dict(ground=add("brick_store", c, STORE_ACCENT[k]), upper=add("brick_window", c),
                                            blank=add("brick", c))
        for k, g in enumerate(GLASS):
            gl = add("glass", (180, 182, 184), g)
            self.styles[f"office{k}"] = dict(ground=gl, upper=gl, blank=add("blank", (180, 182, 184)))
        for k, c in enumerate([(205, 203, 198), (226, 214, 190)]):
            self.styles[f"garage{k}"] = dict(ground=add("garage", c), upper=add("blank", c), blank=add("blank", c))
        conc = add("concrete", (170, 166, 158))
        self.styles["concrete"] = dict(ground=conc, upper=conc, blank=conc)
        wood = add("wood", (120, 98, 76))
        self.styles["wood"] = dict(ground=wood, upper=wood, blank=wood)
        self.railing = add("railing", (236, 236, 234))
        self.cable_railing = add("cable_railing", (60, 62, 64))
        self.slab = add("slab", (222, 220, 214))
        assert n <= self.capacity, f"wall strip too small ({n} > {self.capacity})"
        self.alpha = None

    # ---- style choice -------------------------------------------------
    def style_for(self, osm_id, tags, height, coast_dist=1e9):
        h = sum(ord(ch) for ch in osm_id) * 2654435761 & 0xFFFFFFFF
        r = (h >> 8) % 100
        b = tags.get("building", "yes")
        if b in ("garage", "garages", "carport", "shed", "storage_tank", "roof"):
            return self.styles[f"garage{h % 2}"]
        if b in ("office",) or (b == "commercial" and height > 14):
            return self.styles[f"office{h % 2}"]
        if b in ("retail", "commercial", "supermarket", "kiosk") or "shop" in tags or "amenity" in tags:
            if r < 25:
                return self.styles[f"brick{h % len(BRICK)}"]
            return self.styles[f"shop{h % len(STORE_ACCENT)}"]
        # residential: newer modern boxes dominate near the beach, older
        # stucco and clapboard cottages further up the hill
        p_modern = 40 if coast_dist < 350 else (22 if coast_dist < 900 else 8)
        if r < p_modern:
            return self.styles[f"modern{h % len(MODERN)}"]
        if r < p_modern + 18:
            return self.styles[f"siding{h % len(SIDING)}"]
        return self.styles[f"res{h % len(RESIDENTIAL)}"]

    # ---- rendering ----------------------------------------------------
    def _noise(self, size, amp):
        a = np.random.default_rng(self.rng.randrange(1 << 30)).normal(0, amp, (size, size, 1))
        return a

    def render(self, img, lit):
        """Draw swatches into the bottom strip of `img` (RGB) and `lit` (RGB).

        Also builds `self.alpha` (L image, full texture size; 255 except the
        transparent parts of railing swatches)."""
        sw = self.sw
        alpha = Image.new("L", (self.tex, self.tex), 255)
        for s, kind, col, acc in self._draw:
            tile = np.ones((sw, sw, 3)) * np.array(col, float)
            tile += self._noise(sw, 4.0)
            t = Image.fromarray(np.clip(tile, 0, 255).astype(np.uint8))
            lt = Image.new("RGB", (sw, sw), (0, 0, 0))
            am = None
            d, dl = ImageDraw.Draw(t), ImageDraw.Draw(lt)
            u = sw / 32.0  # 1 unit = bay_width/32
            W = max(1, int(u))
            glass = (55, 68, 80)
            warm = (255, 214, 150)
            dark = tuple(int(c * 0.82) for c in col)

            def window(x0, x1, y0, y1, frame=(250, 250, 248), mull=True):
                d.rectangle([x0 - u, y0 - u, x1 + u, y1 + u], fill=frame)
                d.rectangle([x0, y0, x1, y1], fill=glass)
                if mull:
                    d.line([((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1)], fill=frame, width=W)
                if self.rng.random() < 0.55:
                    dl.rectangle([x0, y0, x1, y1], fill=tuple(int(c * self.rng.uniform(0.6, 1.0)) for c in warm))

            def siding():
                for k in range(0, 33, 2):
                    d.line([(0, k * u), (sw, k * u)], fill=tuple(int(c * 0.86) for c in col), width=W)

            def garage_door(color=(236, 236, 232)):
                d.rectangle([3 * u, 7 * u, 29 * u, 32 * u], fill=color)
                for k in range(10, 32, 3):
                    d.line([(3 * u, k * u), (29 * u, k * u)], fill=tuple(int(c * 0.85) for c in color), width=W)

            if kind in ("window", "door"):
                if kind == "window":
                    window(6 * u, 26 * u, 9 * u, 24 * u)
                else:
                    window(16 * u, 28 * u, 9 * u, 24 * u)
                    d.rectangle([4 * u, 9 * u, 12 * u, 32 * u], fill=(110, 80, 60))
                    d.rectangle([3 * u, 31 * u, 13 * u, 32 * u], fill=(150, 150, 150))
                d.rectangle([0, 30.5 * u, sw, sw], fill=dark)
            elif kind == "garagedoor":
                garage_door()
                d.rectangle([0, 30.5 * u, 3 * u, sw], fill=dark)
            elif kind.startswith("siding"):
                siding()
                if kind == "siding_window":
                    window(8 * u, 24 * u, 8 * u, 24 * u)
                elif kind == "siding_door":
                    window(18 * u, 28 * u, 8 * u, 22 * u)
                    d.rectangle([4 * u, 8 * u, 13 * u, 32 * u], fill=(60, 74, 92))
                elif kind == "siding_garage":
                    garage_door((242, 240, 236))
            elif kind == "modern_window":
                window(1.5 * u, 30.5 * u, 3 * u, 28 * u, frame=(40, 40, 42), mull=False)
                d.line([(16 * u, 3 * u), (16 * u, 28 * u)], fill=(40, 40, 42), width=W)
            elif kind == "modern_ground":
                window(1.5 * u, 22 * u, 3 * u, 32 * u, frame=(40, 40, 42))
                for k in range(3, 32, 2):  # wood slat panel
                    d.line([(23 * u, k * u), (31 * u, k * u)], fill=(150, 112, 78), width=W)
            elif kind == "modern_garage":
                d.rectangle([2 * u, 6 * u, 30 * u, 32 * u], fill=(70, 74, 78))
                for k in range(8, 32, 2):
                    d.line([(2 * u, k * u), (30 * u, k * u)], fill=(95, 98, 102), width=W)
            elif kind.startswith("brick"):
                rows = 0
                for y in range(0, sw, max(2, int(1.2 * u))):
                    off = (rows % 2) * 1.5 * u
                    d.line([(0, y), (sw, y)], fill=(200, 196, 188), width=1)
                    x = -off
                    while x < sw:
                        d.line([(x, y), (x, y + 1.2 * u)], fill=(200, 196, 188), width=1)
                        x += 3 * u
                    rows += 1
                if kind == "brick_window":
                    window(7 * u, 25 * u, 8 * u, 25 * u, frame=(230, 226, 214))
                elif kind == "brick_store":
                    d.rectangle([0, 0, sw, 6 * u], fill=acc)
                    d.rectangle([2 * u, 8 * u, 30 * u, 31 * u], fill=(60, 72, 82))
                    dl.rectangle([2 * u, 8 * u, 30 * u, 31 * u], fill=(255, 225, 175))
            elif kind == "store":
                d.rectangle([0, 0, sw, 7 * u], fill=acc)                      # sign band / awning
                d.rectangle([2 * u, 9 * u, 30 * u, 31 * u], fill=(60, 72, 82))
                d.rectangle([2 * u, 9 * u, 30 * u, 10 * u], fill=(200, 200, 200))
                d.line([(16 * u, 9 * u), (16 * u, 31 * u)], fill=(40, 40, 40), width=W)
                dl.rectangle([2 * u, 10 * u, 30 * u, 31 * u], fill=(255, 225, 175))
                dl.rectangle([0, 0, sw, 7 * u], fill=tuple(min(255, c + 120) for c in acc))
            elif kind == "glass":
                d.rectangle([0, 0, sw, sw], fill=acc)
                for k in range(0, 33, 8):
                    d.line([(k * u, 0), (k * u, sw)], fill=(150, 155, 160), width=W)
                d.rectangle([0, 0, sw, 1.5 * u], fill=(150, 155, 160))
                d.rectangle([0, 30 * u, sw, sw], fill=(150, 155, 160))
                for k in range(4):
                    if self.rng.random() < 0.5:
                        dl.rectangle([k * 8 * u + u, 2 * u, k * 8 * u + 7 * u, 29 * u], fill=(235, 225, 200))
            elif kind == "garage":
                garage_door()
            elif kind == "concrete":
                for k in range(0, 33, 8):
                    d.line([(0, k * u), (sw, k * u)], fill=tuple(int(c * 0.9) for c in col), width=1)
            elif kind == "wood":
                for k in range(0, 33, 2):
                    d.line([(0, k * u), (sw, k * u)], fill=tuple(int(c * 0.8) for c in col), width=1)
            elif kind == "railing":   # white picket-style balcony rail: rails + balusters
                am = Image.new("L", (sw, sw), 0)
                da = ImageDraw.Draw(am)
                da.rectangle([0, 0, sw, 3 * u], fill=255)
                da.rectangle([0, 29 * u, sw, sw], fill=255)
                for x in range(0, sw, max(2, int(2.5 * u))):
                    da.rectangle([x, 0, x + 0.7 * u, sw], fill=255)
            elif kind == "cable_railing":   # dark posts, top rail, horizontal cables
                am = Image.new("L", (sw, sw), 0)
                da = ImageDraw.Draw(am)
                da.rectangle([0, 0, sw, 2.5 * u], fill=255)
                for x in (0, 15 * u, sw - 1.2 * u):
                    da.rectangle([x, 0, x + 1.2 * u, sw], fill=255)
                for y in range(int(6 * u), sw, max(2, int(3.5 * u))):
                    da.line([(0, y), (sw, y)], fill=255, width=1)
            elif kind == "slab":
                d.rectangle([0, 0, sw, 4 * u], fill=tuple(int(c * 0.8) for c in col))
            img.paste(t.filter(ImageFilter.SMOOTH), (s.x, s.y))
            lit.paste(lt, (s.x, s.y))
            if am is not None:
                alpha.paste(am, (s.x, s.y))
        self.alpha = alpha


def make_lit_texture(atlas):
    lit = Image.new("RGB", (atlas.tex, atlas.tex), (0, 0, 0))
    dummy = Image.new("RGB", (atlas.tex, atlas.tex), (0, 0, 0))
    atlas.rng.seed(7)
    atlas.render(dummy, lit)
    return lit
