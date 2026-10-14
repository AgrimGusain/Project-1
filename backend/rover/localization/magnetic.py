"""Magnetometer heading *assessment*.

Raw magnetic-field readings do not directly give a trustworthy compass heading.
A heading is reported only when ALL of these hold:

* the reading is valid and fresh;
* hard/soft-iron calibration has been performed (``calibration.calibrated``);
* the rover is either tilt-compensated (requires IMU orientation fusion, which is
  NOT implemented) or the operator has asserted level operation (``assume_level``);
* the corrected field magnitude is within the expected Earth-field range
  (otherwise local magnetic interference is likely);
* motor interference has been characterised, or the motors are currently idle.

Otherwise the heading is reported as unavailable/unreliable with the reasons.
The magnetic heading is a diagnostic only; it is not used for navigation.
"""

from __future__ import annotations

import math
from typing import Any

from rover.config import MagnetometerSensorConfig
from rover.sensors.base import SensorReading


def corrected_field(raw: tuple[float, float, float], cfg: MagnetometerSensorConfig) -> tuple[float, float, float]:
    cal = cfg.calibration
    v = [raw[i] - cal.hard_iron_ut[i] for i in range(3)]
    m = cal.soft_iron
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))  # type: ignore[return-value]


def assess_heading(
    reading: SensorReading | None,
    cfg: MagnetometerSensorConfig,
    *,
    motors_active: bool,
    tilt_compensation_available: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    if reading is None or not reading.valid:
        return {"available": False, "reliable": False, "heading_deg": None,
                "reasons": ["no valid magnetometer reading"], "field_ut": None}
    raw = (float(reading.value["x"]), float(reading.value["y"]), float(reading.value["z"]))
    cal = cfg.calibration
    if not cal.calibrated:
        reasons.append("magnetometer not calibrated (hard/soft-iron)")
    if not tilt_compensation_available and not cfg.assume_level:
        reasons.append("no tilt compensation (IMU orientation fusion not available) and level operation not asserted")
    bx, by, bz = corrected_field(raw, cfg) if cal.calibrated else raw
    magnitude = math.sqrt(bx * bx + by * by + bz * bz)
    lo, hi = cfg.expected_field_ut
    if not (lo <= magnitude <= hi):
        reasons.append(f"field magnitude {magnitude:.1f} uT outside expected {lo:.0f}-{hi:.0f} uT (interference likely)")
    if motors_active and not cal.motor_interference_checked:
        reasons.append("motors running and motor interference has not been characterised")
    reliable = not reasons
    heading = None
    if reliable:
        heading = (math.degrees(math.atan2(-bx, by)) + cfg.declination_deg + cfg.mount_yaw_deg) % 360.0
    return {
        "available": True,
        "reliable": reliable,
        "heading_deg": None if heading is None else round(heading, 2),
        "reasons": reasons,
        "field_ut": {"x": round(bx, 3), "y": round(by, 3), "z": round(bz, 3), "magnitude": round(magnitude, 3)},
        "calibrated": cal.calibrated,
    }
