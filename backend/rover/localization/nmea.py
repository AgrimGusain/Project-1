"""Minimal, dependency-free NMEA 0183 parser (GGA, RMC, GST, GSA).

Works with any talker ID (GP, GN, GL, GA, BD...). Sentences with a missing or
wrong checksum are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NmeaError(ValueError):
    pass


def checksum(body: str) -> int:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return c


def _split(line: str) -> list[str]:
    line = line.strip()
    if not line.startswith("$"):
        raise NmeaError("not an NMEA sentence")
    if "*" not in line:
        raise NmeaError("missing checksum")
    body, _, cs = line[1:].partition("*")
    try:
        expected = int(cs[:2], 16)
    except ValueError as exc:
        raise NmeaError("malformed checksum") from exc
    if checksum(body) != expected:
        raise NmeaError("checksum mismatch")
    return body.split(",")


def _coord(value: str, hemi: str, deg_digits: int) -> float | None:
    if not value or not hemi:
        return None
    try:
        deg = int(value[:deg_digits])
        minutes = float(value[deg_digits:])
    except ValueError as exc:
        raise NmeaError(f"bad coordinate {value!r}") from exc
    if minutes >= 60:
        raise NmeaError("minutes out of range")
    out = deg + minutes / 60.0
    if hemi in ("S", "W"):
        out = -out
    elif hemi not in ("N", "E"):
        raise NmeaError("bad hemisphere")
    return out


def _f(v: str) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _i(v: str) -> int | None:
    try:
        return int(v) if v else None
    except ValueError:
        return None


@dataclass
class Sentence:
    type: str
    data: dict[str, Any]


def parse(line: str) -> Sentence | None:
    """Parse one sentence. Returns None for unsupported types; raises NmeaError if malformed."""
    f = _split(line)
    tag = f[0]
    if len(tag) < 5:
        raise NmeaError("bad sentence tag")
    kind = tag[-3:]
    if kind == "GGA":
        if len(f) < 10:
            raise NmeaError("short GGA")
        return Sentence("GGA", {
            "time": f[1],
            "lat": _coord(f[2], f[3], 2),
            "lon": _coord(f[4], f[5], 3),
            "quality": _i(f[6]) or 0,
            "satellites": _i(f[7]),
            "hdop": _f(f[8]),
            "alt": _f(f[9]),
        })
    if kind == "RMC":
        if len(f) < 10:
            raise NmeaError("short RMC")
        knots = _f(f[7])
        return Sentence("RMC", {
            "time": f[1],
            "valid": f[2] == "A",
            "lat": _coord(f[3], f[4], 2),
            "lon": _coord(f[5], f[6], 3),
            "speed_mps": None if knots is None else knots * 0.514444,
            "course_deg": _f(f[8]),
            "date": f[9],
        })
    if kind == "GST":
        if len(f) < 9:
            raise NmeaError("short GST")
        return Sentence("GST", {"time": f[1], "rms": _f(f[2]), "std_lat": _f(f[6]), "std_lon": _f(f[7]), "std_alt": _f(f[8])})
    if kind == "GSA":
        if len(f) < 18:
            raise NmeaError("short GSA")
        return Sentence("GSA", {"fix_type": _i(f[2]), "pdop": _f(f[15]), "hdop": _f(f[16]), "vdop": _f(f[17])})
    return None


def build(body: str) -> str:
    """Build a sentence with checksum (used by tests and the simulator)."""
    return f"${body}*{checksum(body):02X}"
