"""Obstacle assessment from all sensors that declare a ``safety_role``.

Obstacle sensors are *not* landmine detectors; they only gate motion.

Rules:
* A missing, stale or invalid measurement is never treated as a clear path.
  With ``block_forward_on_sensor_invalid`` it blocks motion in the guarded direction.
* If *no* sensor guards a direction (operator removed them from the config), the
  state is DISABLED: motion is not blocked, but the obstacle-sensing capability is
  reported unavailable and the UI warns the operator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rover.config import SafetyConfig
from rover.core.enums import ObstacleState
from rover.sensors.base import ConnectionState, ReadingHealth
from rover.sensors.manager import SensorChannel, SensorManager

_SEVERITY = {
    ObstacleState.OBSTACLE: 5,
    ObstacleState.UNAVAILABLE: 4,
    ObstacleState.STALE: 3,
    ObstacleState.INVALID: 2,
    ObstacleState.CLEAR: 1,
    ObstacleState.DISABLED: 0,
}


@dataclass
class DirectionAssessment:
    state: ObstacleState = ObstacleState.DISABLED
    distance_m: float | None = None
    blocked: bool = False
    slow: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "distance_m": self.distance_m,
            "blocked": self.blocked,
            "slow": self.slow,
            "reasons": self.reasons,
        }


@dataclass
class ObstacleAssessment:
    front: DirectionAssessment
    rear: DirectionAssessment
    sensors: list[dict[str, Any]]

    @property
    def block_forward(self) -> bool:
        return self.front.blocked

    @property
    def block_reverse(self) -> bool:
        return self.rear.blocked

    def to_dict(self) -> dict[str, Any]:
        return {"front": self.front.to_dict(), "rear": self.rear.to_dict(), "sensors": self.sensors}


def _sensor_state(ch: SensorChannel, cfg: SafetyConfig, now: float) -> tuple[ObstacleState, float | None, str]:
    label = ch.cfg.label or ch.id
    if ch.state != ConnectionState.CONNECTED:
        return ObstacleState.UNAVAILABLE, None, f"{label}: {ch.state.value}"
    health = ch.reading_health(now)
    if health in (ReadingHealth.NONE, ReadingHealth.STALE):
        return ObstacleState.STALE, None, f"{label}: no recent measurement"
    reading = ch.last_reading
    assert reading is not None
    if health == ReadingHealth.INVALID:
        return ObstacleState.INVALID, None, f"{label}: invalid ({reading.error})"
    if ch.type == "ultrasonic":
        if reading.field("beyond_range"):
            return ObstacleState.CLEAR, None, ""
        d = reading.field("distance")
        if d is None:
            return ObstacleState.INVALID, None, f"{label}: no distance"
        if d <= cfg.obstacle_stop_distance_m:
            return ObstacleState.OBSTACLE, d, f"{label}: obstacle at {d:.2f} m"
        return ObstacleState.CLEAR, d, ""
    if ch.type == "ir_obstacle":
        detected = reading.field("obstacle")
        if detected is None:
            return ObstacleState.INVALID, None, f"{label}: invalid level"
        return (ObstacleState.OBSTACLE, None, f"{label}: obstacle detected") if detected else (ObstacleState.CLEAR, None, "")
    return ObstacleState.DISABLED, None, ""


def assess_obstacles(sensors: SensorManager, cfg: SafetyConfig) -> ObstacleAssessment:
    now = time.monotonic()
    result = {"front": DirectionAssessment(), "rear": DirectionAssessment()}
    per_sensor: list[dict[str, Any]] = []
    for ch in sensors.channels.values():
        role = getattr(ch.cfg, "safety_role", "none")
        if not ch.cfg.enabled or role not in result:
            continue
        state, dist, reason = _sensor_state(ch, cfg, now)
        per_sensor.append({"id": ch.id, "role": role, "state": state.value, "distance_m": dist})
        da = result[role]
        if _SEVERITY[state] > _SEVERITY[da.state]:
            da.state = state
        if reason:
            da.reasons.append(reason)
        if dist is not None and (da.distance_m is None or dist < da.distance_m):
            da.distance_m = dist
    for da in result.values():
        if da.state == ObstacleState.OBSTACLE:
            da.blocked = True
        elif da.state in (ObstacleState.UNAVAILABLE, ObstacleState.STALE, ObstacleState.INVALID):
            da.blocked = cfg.block_forward_on_sensor_invalid
        if da.distance_m is not None and da.distance_m <= cfg.obstacle_slow_distance_m:
            da.slow = True
    return ObstacleAssessment(result["front"], result["rear"], per_sensor)
