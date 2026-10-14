"""Dubins shortest paths (forward-only, minimum turn radius).

Math frame internally: x right/east, y up/north, angle ``theta`` counter-clockwise
from +x. Callers convert from compass headings with ``theta = 90deg - heading``.
Formulas follow Shkel & Lumelsky (2001), as in A. Walker's reference implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TWO_PI = 2 * math.pi


def mod2pi(a: float) -> float:
    return a - TWO_PI * math.floor(a / TWO_PI)


@dataclass(frozen=True)
class DubinsPath:
    word: str  # e.g. "LSL"
    params: tuple[float, float, float]  # normalised lengths (radians for arcs, d/r for straight)
    radius: float

    @property
    def length(self) -> float:
        return sum(self.params) * self.radius


def _candidates(alpha: float, beta: float, d: float) -> dict[str, tuple[float, float, float] | None]:
    sa, sb, ca, cb = math.sin(alpha), math.sin(beta), math.cos(alpha), math.cos(beta)
    c_ab = math.cos(alpha - beta)
    d_sq = d * d
    out: dict[str, tuple[float, float, float] | None] = {}

    p_sq = 2 + d_sq - 2 * c_ab + 2 * d * (sa - sb)
    if p_sq >= 0:
        tmp1 = math.atan2(cb - ca, d + sa - sb)
        out["LSL"] = (mod2pi(tmp1 - alpha), math.sqrt(p_sq), mod2pi(beta - tmp1))
    p_sq = 2 + d_sq - 2 * c_ab + 2 * d * (sb - sa)
    if p_sq >= 0:
        tmp1 = math.atan2(ca - cb, d - sa + sb)
        out["RSR"] = (mod2pi(alpha - tmp1), math.sqrt(p_sq), mod2pi(tmp1 - beta))
    p_sq = -2 + d_sq + 2 * c_ab + 2 * d * (sa + sb)
    if p_sq >= 0:
        p = math.sqrt(p_sq)
        tmp0 = math.atan2(-ca - cb, d + sa + sb) - math.atan2(-2.0, p)
        out["LSR"] = (mod2pi(tmp0 - alpha), p, mod2pi(tmp0 - mod2pi(beta)))
    p_sq = -2 + d_sq + 2 * c_ab - 2 * d * (sa + sb)
    if p_sq >= 0:
        p = math.sqrt(p_sq)
        tmp0 = math.atan2(ca + cb, d - sa - sb) - math.atan2(2.0, p)
        out["RSL"] = (mod2pi(alpha - tmp0), p, mod2pi(beta - tmp0))
    tmp0 = (6.0 - d_sq + 2 * c_ab + 2 * d * (sa - sb)) / 8.0
    if abs(tmp0) <= 1:
        phi = math.atan2(ca - cb, d - sa + sb)
        p = mod2pi(TWO_PI - math.acos(tmp0))
        t = mod2pi(alpha - phi + mod2pi(p / 2.0))
        out["RLR"] = (t, p, mod2pi(alpha - beta - t + mod2pi(p)))
    tmp0 = (6.0 - d_sq + 2 * c_ab + 2 * d * (sb - sa)) / 8.0
    if abs(tmp0) <= 1:
        phi = math.atan2(ca - cb, d + sa - sb)
        p = mod2pi(TWO_PI - math.acos(tmp0))
        t = mod2pi(-alpha - phi + p / 2.0)
        out["LRL"] = (t, p, mod2pi(mod2pi(beta) - alpha - t + mod2pi(p)))
    return out


def shortest_path(q0: tuple[float, float, float], q1: tuple[float, float, float], radius: float) -> DubinsPath:
    """q = (x, y, theta) in the math frame."""
    if radius <= 0:
        raise ValueError("radius must be positive")
    dx, dy = q1[0] - q0[0], q1[1] - q0[1]
    dist = math.hypot(dx, dy)
    d = dist / radius
    th = mod2pi(math.atan2(dy, dx)) if dist > 1e-12 else 0.0
    alpha = mod2pi(q0[2] - th)
    beta = mod2pi(q1[2] - th)
    best: DubinsPath | None = None
    # Deterministic order for ties.
    for word in ("LSL", "RSR", "LSR", "RSL", "RLR", "LRL"):
        params = _candidates(alpha, beta, d).get(word)
        if params is None:
            continue
        cand = DubinsPath(word, params, radius)
        if best is None or cand.length < best.length - 1e-12:
            best = cand
    if best is None:  # pragma: no cover - a Dubins path always exists
        raise ValueError("no Dubins path")
    return best


def step(q: tuple[float, float, float], kind: str, amount: float, radius: float) -> tuple[float, float, float]:
    """Advance pose q by one primitive. ``amount`` is radians for L/R, metres for S."""
    x, y, th = q
    if kind == "S":
        return x + amount * math.cos(th), y + amount * math.sin(th), th
    if kind == "L":
        cx, cy = x - radius * math.sin(th), y + radius * math.cos(th)
        th2 = th + amount
        return cx + radius * math.sin(th2), cy - radius * math.cos(th2), th2
    if kind == "R":
        cx, cy = x + radius * math.sin(th), y - radius * math.cos(th)
        th2 = th - amount
        return cx - radius * math.sin(th2), cy + radius * math.cos(th2), th2
    raise ValueError(kind)


def turn_center(q: tuple[float, float, float], kind: str, radius: float) -> tuple[float, float]:
    x, y, th = q
    if kind == "L":
        return x - radius * math.sin(th), y + radius * math.cos(th)
    return x + radius * math.sin(th), y - radius * math.cos(th)
