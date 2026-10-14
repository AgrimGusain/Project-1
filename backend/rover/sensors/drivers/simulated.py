"""Simulated sensor drivers. Every reading they produce carries ``simulated=True``."""

from __future__ import annotations

import math
from typing import Any

from rover.sensors.base import Sensor, SensorError, SensorReading
from rover.sensors.drivers import DriverContext, register
from rover.sim.world import SimWorld


class _SimSensor(Sensor):
    simulated = True
    physically_verified = False

    def __init__(self, config: Any, ctx: DriverContext) -> None:
        super().__init__(config, ctx)
        assert ctx.world is not None
        self.world: SimWorld = ctx.world


@register
class SimUltrasonic(_SimSensor):
    sensor_type = "ultrasonic"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        c = self.config
        w = self.world
        if w.has_fault("ultrasonic_disconnect"):
            raise SensorError("simulated I/O error")
        if w.has_fault("ultrasonic_stale"):
            return None
        units = {"distance": "m", "beyond_range": "bool"}
        if w.has_fault("ultrasonic_no_echo"):
            return self.reading({"distance": None, "beyond_range": None}, units, valid=False, error="no_echo")
        d = w.ray_distance(c.mount, c.beam_half_angle_deg, c.max_range_m)
        if d is None:
            # Like a real HC-SR04 timing out normally: a valid "nothing within range" result.
            return self.reading({"distance": None, "beyond_range": True}, units)
        d = max(c.min_range_m, d + w.gauss(self.ctx.rover_config.simulation.ultrasonic_noise_m))
        return self.reading({"distance": round(d, 4), "beyond_range": False}, units)


@register
class SimIrObstacle(_SimSensor):
    sensor_type = "ir_obstacle"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        c = self.config
        if self.world.has_fault("ir_fault"):
            return self.reading({"obstacle": None}, {"obstacle": "bool"}, valid=False, error="invalid_level")
        d = self.world.ray_distance(c.mount, 5.0, c.nominal_range_m, rays=3)
        detected = d is not None
        level = int(not detected) if c.active_low else int(detected)
        return self.reading({"obstacle": detected, "raw_level": level}, {"obstacle": "bool", "raw_level": "logic"})


@register
class SimMetalDetector(_SimSensor):
    sensor_type = "metal_detector"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        c = self.config
        signal = self.world.metal_signal(c.mount)
        return self.reading(
            {"signal": round(signal, 4), "triggered": signal >= c.threshold},
            {"signal": "normalized", "triggered": "bool"},
        )

    def calibration(self) -> dict[str, Any]:
        return {"state": "simulated", **self.config.calibration}


@register
class SimMagnetometer(_SimSensor):
    """Simulated 3-axis magnetometer: Earth field + hard-iron offset + motor interference."""

    sensor_type = "magnetometer"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        if self.world.has_fault("magnetometer_disconnect"):
            raise SensorError("simulated I2C NACK")
        x, y, z = self.world.magnetic_field_body()
        mag = math.sqrt(x * x + y * y + z * z)
        return self.reading(
            {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3), "magnitude": round(mag, 3)},
            {"x": "uT", "y": "uT", "z": "uT", "magnitude": "uT"},
            frame="body_xright_yforward_zup",
        )

    def calibration(self) -> dict[str, Any]:
        cal = self.config.calibration
        return {"state": "calibrated" if cal.calibrated else "uncalibrated", **cal.model_dump()}


@register
class SimImu(_SimSensor):
    sensor_type = "imu"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        data = self.world.imu()
        gx, gy, gz = data["gyro_dps"]
        ax, ay, az = data["accel_mps2"]
        return self.reading(
            {"gyro_x": gx, "gyro_y": gy, "gyro_z": gz, "accel_x": ax, "accel_y": ay, "accel_z": az},
            {**{k: "deg/s" for k in ("gyro_x", "gyro_y", "gyro_z")}, **{k: "m/s^2" for k in ("accel_x", "accel_y", "accel_z")}},
            frame="body_xright_yforward_zup",
        )

    def calibration(self) -> dict[str, Any]:
        return {"state": "calibrated" if self.config.calibrated else "uncalibrated", "gyro_bias_dps": list(self.config.gyro_bias_dps)}


@register
class SimWheelEncoder(_SimSensor):
    sensor_type = "wheel_encoder"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        c = self.config
        travel = self.world.wheel_travel(c.side)
        counts = int(travel / (math.pi * c.wheel_diameter_m) * c.counts_per_rev)
        return self.reading(
            {"counts": counts},
            {"counts": "count"},
            side=c.side,
            counts_per_rev=c.counts_per_rev,
            wheel_diameter_m=c.wheel_diameter_m,
        )

    def calibration(self) -> dict[str, Any]:
        return {"state": "calibrated" if self.config.calibrated else "uncalibrated"}


@register
class SimBattery(_SimSensor):
    sensor_type = "battery"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        v, a = self.world.battery(self.config.cells)
        return self.reading({"voltage": round(v, 3), "current": round(a, 3)}, {"voltage": "V", "current": "A"})


@register
class SimTemperature(_SimSensor):
    sensor_type = "temperature"
    driver_name = "simulated"

    def acquire(self) -> SensorReading | None:
        return self.reading({"temperature": round(48.0 + self.world.gauss(0.3), 2)}, {"temperature": "degC"})
