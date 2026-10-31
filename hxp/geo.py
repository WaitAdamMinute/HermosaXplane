"""Coordinate helpers: WGS84 <-> Web Mercator pixels <-> local metric frame."""
import math

EARTH_R = 6378137.0


def merc_px(lon, lat, zoom):
    """Global Web-Mercator pixel coordinates (256 px tiles) at `zoom`."""
    n = 256.0 * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def px_lonlat(x, y, zoom):
    n = 256.0 * (2 ** zoom)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def px_to_merc_m(x, y, zoom):
    """Global pixel -> EPSG:3857 metres."""
    res = 2 * math.pi * EARTH_R / (256.0 * (2 ** zoom))
    return x * res - math.pi * EARTH_R, math.pi * EARTH_R - y * res


class LocalFrame:
    """Equirectangular east/north metres around an origin (good to cm over a few km)."""

    def __init__(self, lon0, lat0):
        self.lon0 = lon0
        self.lat0 = lat0
        self.kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_R

    def to_xy(self, lon, lat):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def to_ll(self, x, y):
        return self.lon0 + x / self.kx, self.lat0 + y / self.ky


def tile_name(lon, lat):
    """DSF tile and folder name, e.g. ('+30-120', '+33-119')."""
    lo, la = math.floor(lon), math.floor(lat)
    fo, fa = math.floor(lo / 10) * 10, math.floor(la / 10) * 10
    return f"{fa:+03d}{fo:+04d}", f"{la:+03d}{lo:+04d}"
