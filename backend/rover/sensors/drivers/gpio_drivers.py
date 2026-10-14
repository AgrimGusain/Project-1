"""Raspberry Pi GPIO sensor drivers (lgpio).

These drivers are implemented against the documented device behaviour but are
NOT yet verified on the physical rover (``physically_verified = False``). See
docs/SENSORS.md for the verification procedure.

ELECTRICAL WARNING: the HC-SR04 Echo pin outputs 5 V. It MUST pass through a
level shifter or a voltage divider (e.g. 1 kOhm series / 2 kOhm to GND) before
reaching a Pi GPIO input, which tolerates 3.3 V only.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from rover.hal.gpio import GpioChip, HardwareInitError
from rover.sensors.base import Sensor, SensorError, SensorInitError, SensorReading
from rover.sensors.drivers import DriverContext, register


_DIST_NONE = {"distance": None, "beyond_range": None}
_DIST_UNITS = {"distance": "m", "beyond_range": "bool"}


class _GpioSensor(Sensor):
    simulated = False
    physically_verified = False

    def __init__(self, config: Any, ctx: DriverContext) -> None:
        super().__init__(config, ctx)
        self.chip: GpioChip | None = None

    def _chip(self) -> GpioChip:
        if self.ctx.gpio is None:
            raise SensorInitError("GPIO is not available in this mode")
        try:
            return self.ctx.gpio.get()
        except HardwareInitError as exc:
            raise SensorInitError(str(exc)) from exc


@register
class HcSr04(_GpioSensor):
    sensor_type = "ultrasonic"
    driver_name = "hcsr04"

    def __init__(self, config: Any, ctx: DriverContext) -> None:
        super().__init__(config, ctx)
        self._cb: Any = None
        self._rise_ns: int | None = None
        self._width_ns: int | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()

    def initialize(self) -> None:
        c = self.config
        chip = self._chip()
        try:
            chip.claim_output(c.trigger_pin, 0)
            chip.claim_alert(c.echo_pin, "down")
            self._cb = chip.callback(c.echo_pin, self._edge)
        except Exception as exc:
            self.close()
            raise SensorInitError(f"HC-SR04 GPIO setup failed: {exc}") from exc
        self.chip = chip
        time.sleep(0.05)

    def _edge(self, _chip: int, _gpio: int, level: int, tick_ns: int) -> None:
        if level == 1:
            self._rise_ns = tick_ns
        elif level == 0 and self._rise_ns is not None:
            self._width_ns = tick_ns - self._rise_ns
            self._done.set()

    def acquire(self) -> SensorReading | None:
        c = self.config
        if self.chip is None:
            raise SensorError("not initialised")
        with self._lock:
            self._done.clear()
            self._rise_ns = None
            self._width_ns = None
            try:
                self.chip.write(c.trigger_pin, 1)
                t_end = time.perf_counter() + 12e-6
                while time.perf_counter() < t_end:
                    pass
                self.chip.write(c.trigger_pin, 0)
            except Exception as exc:
                raise SensorError(f"trigger write failed: {exc}") from exc
            # An HC-SR04 holds Echo high for ~38 ms when nothing returns; allow margin.
            if not self._done.wait(0.05):
                if self._rise_ns is None:
                    # No echo pulse at all: wiring, power or level-shifter problem.
                    return self.reading(_DIST_NONE, _DIST_UNITS, valid=False, error="no_echo")
                return self.reading(_DIST_NONE, _DIST_UNITS, valid=False, error="echo_stuck_high")
            width_s = (self._width_ns or 0) / 1e9
        speed = 331.3 + 0.606 * c.temperature_c
        distance = width_s * speed / 2.0
        if distance > c.max_range_m:
            # The sensor fired and timed out normally: a valid "nothing within range" result.
            return self.reading(
                {"distance": None, "beyond_range": True}, _DIST_UNITS, echo_us=round(width_s * 1e6, 1)
            )
        if distance < c.min_range_m:
            return self.reading(
                _DIST_NONE, _DIST_UNITS, valid=False, error="below_min_range", raw_distance_m=round(distance, 4)
            )
        return self.reading(
            {"distance": round(distance, 4), "beyond_range": False}, _DIST_UNITS, echo_us=round(width_s * 1e6, 1)
        )

    def close(self) -> None:
        if self._cb is not None:
            try:
                self._cb.cancel()
            except Exception:
                pass
            self._cb = None
        if self.chip is not None:
            self.chip.free(self.config.trigger_pin)
            self.chip.free(self.config.echo_pin)
            self.chip = None

    def calibration(self) -> dict[str, Any]:
        return {"state": "not_applicable", "temperature_c": self.config.temperature_c}


class _DigitalInputSensor(_GpioSensor):
    """Debounced digital input. Electrical polarity is configurable (active_low/pull)."""

    field_name = "state"

    def __init__(self, config: Any, ctx: DriverContext) -> None:
        super().__init__(config, ctx)
        self._stable: int | None = None
        self._candidate: int | None = None
        self._since = 0.0

    def initialize(self) -> None:
        chip = self._chip()
        try:
            chip.claim_input(self.config.pin, self.config.pull)
        except Exception as exc:
            raise SensorInitError(f"GPIO{self.config.pin} input setup failed: {exc}") from exc
        self.chip = chip

    def _debounced_level(self) -> int:
        if self.chip is None:
            raise SensorError("not initialised")
        try:
            level = self.chip.read(self.config.pin)
        except Exception as exc:
            raise SensorError(f"GPIO read failed: {exc}") from exc
        now = time.monotonic()
        debounce = getattr(self.config, "debounce_ms", 0.0) / 1000.0
        if level != self._candidate:
            self._candidate, self._since = level, now
        if self._stable is None or (level != self._stable and now - self._since >= debounce):
            self._stable = level
        return self._stable

    def _active(self, level: int) -> bool:
        return (level == 0) if self.config.active_low else (level == 1)

    def close(self) -> None:
        if self.chip is not None:
            self.chip.free(self.config.pin)
            self.chip = None


@register
class GpioIrObstacle(_DigitalInputSensor):
    sensor_type = "ir_obstacle"
    driver_name = "gpio"

    def acquire(self) -> SensorReading | None:
        level = self._debounced_level()
        return self.reading(
            {"obstacle": self._active(level), "raw_level": level},
            {"obstacle": "bool", "raw_level": "logic"},
            active_low=self.config.active_low,
        )


@register
class GpioDigitalMetalDetector(_DigitalInputSensor):
    """Metal-detector module with a digital (comparator) output. Signal is 0 or 1."""

    sensor_type = "metal_detector"
    driver_name = "gpio_digital"

    def acquire(self) -> SensorReading | None:
        level = self._debounced_level()
        active = self._active(level)
        return self.reading(
            {"signal": 1.0 if active else 0.0, "triggered": active, "raw_level": level},
            {"signal": "normalized", "triggered": "bool", "raw_level": "logic"},
        )

    def calibration(self) -> dict[str, Any]:
        cal = self.config.calibration
        return {"state": "calibrated" if cal.get("calibrated") else "uncalibrated", **cal}
