"""WGS-84 <-> local East-North-Up (ENU) tangent plane.

Coverage paths are never computed in latitude/longitude degrees. Geographic
boundaries are projected into a local ENU frame centred on an origin near the
survey area (x = east, y = north, metres), planned there, and converted back.

The flat tangent plane is accurate to millimetres over a few hundred metres and
to well under a decimetre over ~2 km, which is far larger than any survey this
rover can perform. ``localization.geo_max_extent_m`` enforces the limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_A = 6378137.0  # WGS-84 semi-major axis
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def geodetic_to_ecef(lat: float, lon: float, alt: float = 0.0) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    x = (n + alt) * math.cos(la) * math.cos(lo)
    y = (n + alt) * math.cos(la) * math.sin(lo)
    z = (n * (1 - _E2) + alt) * math.sin(la)
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _E2))
    alt = 0.0
    for _ in range(8):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


@dataclass(frozen=True)
class EnuFrame:
    lat0: float
    lon0: float
    alt0: float = 0.0

    def _rot(self) -> tuple[float, float, float, float]:
        la, lo = math.radians(self.lat0), math.radians(self.lon0)
        return math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)

    def to_enu(self, lat: float, lon: float, alt: float | None = None) -> tuple[float, float]:
        alt = self.alt0 if alt is None else alt
        x, y, z = geodetic_to_ecef(lat, lon, alt)
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.alt0)
        dx, dy, dz = x - x0, y - y0, z - z0
        sla, cla, slo, clo = self._rot()
        east = -slo * dx + clo * dy
        north = -sla * clo * dx - sla * slo * dy + cla * dz
        return east, north

    def to_geodetic(self, east: float, north: float, up: float = 0.0) -> tuple[float, float]:
        sla, cla, slo, clo = self._rot()
        dx = -slo * east - sla * clo * north + cla * clo * up
        dy = clo * east - sla * slo * north + cla * slo * up
        dz = cla * north + sla * up
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.alt0)
        lat, lon, _ = ecef_to_geodetic(x0 + dx, y0 + dy, z0 + dz)
        return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
