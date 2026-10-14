"""Motor drivers.

``set_outputs(left, right)`` takes signed outputs in [-1, 1]. Only the
:class:`~rover.motion.controller.MotionController` calls it.

:class:`WatchdogMotorDriver` wraps every driver with an independent OS thread that
forces zero output if no update arrives within ``watchdog_timeout_s``. This keeps
the motors from running if the asyncio loop or the control task hangs.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from rover.config import MotorConfig, MotorSideConfig
from rover.hal.gpio import HardwareInitError, LazyGpio
from rover.sim.world import SimWorld

log = logging.getLogger(__name__)


def _validate(value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid motor output {value!r}")
    return max(-1.0, min(1.0, float(value)))


class MotorDriver(ABC):
    driver_name = "abstract"
    simulated = False

    def __init__(self) -> None:
        self.outputs = (0.0, 0.0)
        self.initialized = False
        self.error: str | None = None

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def _apply(self, left: float, right: float) -> None: ...

    def set_outputs(self, left: float, right: float) -> None:
        left, right = _validate(left), _validate(right)
        self._apply(left, right)
        self.outputs = (left, right)

    def stop(self) -> None:
        try:
            self._apply(0.0, 0.0)
        finally:
            self.outputs = (0.0, 0.0)

    def close(self) -> None:
        self.stop()

    def status(self) -> dict[str, Any]:
        return {
            "driver": self.driver_name,
            "simulated": self.simulated,
            "initialized": self.initialized,
            "healthy": self.initialized and self.error is None,
            "error": self.error,
            "outputs": {"left": round(self.outputs[0], 3), "right": round(self.outputs[1], 3)},
        }


class SimMotorDriver(MotorDriver):
    driver_name = "simulated"
    simulated = True

    def __init__(self, world: SimWorld) -> None:
        super().__init__()
        self.world = world

    def initialize(self) -> None:
        self.world.set_motor_outputs(0.0, 0.0)
        self.initialized = True

    def _apply(self, left: float, right: float) -> None:
        self.world.set_motor_outputs(left, right)

    def status(self) -> dict[str, Any]:
        st = super().status()
        if self.world.has_fault("motor_driver_fault"):
            st["healthy"] = False
            st["error"] = "simulated driver fault injected"
        return st


class L298NMotorDriver(MotorDriver):
    """L298N dual H-bridge: IN1/IN2 (+ENA PWM) for the left side, IN3/IN4 (+ENB) for the right.

    Zero output drives both direction inputs low with 0 % duty (coast). Fit
    pull-down resistors on IN1-IN4/ENA/ENB so the bridge is disabled whenever the
    Pi is not actively driving the lines (boot, crash, process exit).
    """

    driver_name = "l298n"
    simulated = False

    def __init__(self, cfg: MotorConfig, gpio: LazyGpio) -> None:
        super().__init__()
        self.cfg = cfg
        self.gpio = gpio
        self._chip: Any = None

    def initialize(self) -> None:
        try:
            chip = self.gpio.get()
            for side in (self.cfg.left, self.cfg.right):
                chip.claim_output(side.in_a, 0)
                chip.claim_output(side.in_b, 0)
                chip.claim_output(side.enable, 0)
                chip.pwm(side.enable, self.cfg.pwm_frequency_hz, 0.0)
        except Exception as exc:
            self.error = str(exc)
            raise HardwareInitError(f"L298N initialisation failed: {exc}") from exc
        self._chip = chip
        self.initialized = True
        self.error = None

    def _duty(self, value: float) -> float:
        mag = abs(value)
        if mag < 0.02:
            return 0.0
        return 100.0 * (self.cfg.min_duty + (1.0 - self.cfg.min_duty) * mag)

    def _side(self, side: MotorSideConfig, value: float) -> None:
        if side.invert:
            value = -value
        chip = self._chip
        duty = self._duty(value)
        if duty == 0.0:
            chip.pwm(side.enable, self.cfg.pwm_frequency_hz, 0.0)
            chip.write(side.in_a, 0)
            chip.write(side.in_b, 0)
            return
        forward = value > 0
        chip.write(side.in_a, 1 if forward else 0)
        chip.write(side.in_b, 0 if forward else 1)
        chip.pwm(side.enable, self.cfg.pwm_frequency_hz, duty)

    def _apply(self, left: float, right: float) -> None:
        if self._chip is None:
            if left == 0.0 and right == 0.0:
                return
            raise HardwareInitError("L298N not initialised")
        try:
            self._side(self.cfg.left, left)
            self._side(self.cfg.right, right)
        except Exception as exc:
            self.error = f"GPIO write failed: {exc}"
            raise

    def close(self) -> None:
        if self._chip is None:
            return
        try:
            self.stop()
        finally:
            for side in (self.cfg.left, self.cfg.right):
                for pin in (side.enable, side.in_a, side.in_b):
                    self._chip.free(pin)
            self._chip = None
            self.initialized = False


class WatchdogMotorDriver(MotorDriver):
    """Thread-based watchdog in front of a real or simulated driver."""

    def __init__(self, inner: MotorDriver, timeout_s: float, on_trip: Any = None) -> None:
        super().__init__()
        self.inner = inner
        self.driver_name = inner.driver_name
        self.simulated = inner.simulated
        self.timeout_s = timeout_s
        self.on_trip = on_trip
        self._lock = threading.Lock()
        self._last_update = time.monotonic()
        self._tripped = False
        self.trip_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="motor-watchdog", daemon=True)

    def initialize(self) -> None:
        self.inner.initialize()
        self.initialized = True
        self._last_update = time.monotonic()
        self._thread.start()

    def _apply(self, left: float, right: float) -> None:  # pragma: no cover - not used
        raise NotImplementedError

    def set_outputs(self, left: float, right: float) -> None:
        with self._lock:
            self.inner.set_outputs(left, right)
            self.outputs = self.inner.outputs
            self._last_update = time.monotonic()
            self._tripped = False

    def stop(self) -> None:
        with self._lock:
            self.inner.stop()
            self.outputs = (0.0, 0.0)
            self._last_update = time.monotonic()

    def _watch(self) -> None:
        period = min(0.05, self.timeout_s / 4)
        while not self._stop.wait(period):
            trip = False
            with self._lock:
                stale = time.monotonic() - self._last_update > self.timeout_s
                if stale and not self._tripped:
                    try:
                        self.inner.stop()
                    except Exception:  # pragma: no cover
                        log.exception("watchdog failed to stop motors")
                    self.outputs = (0.0, 0.0)
                    self._tripped = True
                    self.trip_count += 1
                    trip = True
            if trip and self.on_trip:
                try:
                    self.on_trip()
                except Exception:  # pragma: no cover
                    log.exception("watchdog trip callback failed")

    @property
    def tripped(self) -> bool:
        return self._tripped

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        with self._lock:
            try:
                self.inner.close()
            except Exception:  # pragma: no cover
                log.exception("motor driver close failed")
            self.outputs = (0.0, 0.0)

    def status(self) -> dict[str, Any]:
        st = self.inner.status()
        st.update({"watchdog_timeout_s": self.timeout_s, "watchdog_tripped": self._tripped, "watchdog_trips": self.trip_count})
        return st


class UnavailableMotorDriver(MotorDriver):
    """Stands in when real motor hardware failed to initialise. Refuses every non-zero command."""

    def __init__(self, driver_name: str, error: str) -> None:
        super().__init__()
        self.driver_name = driver_name
        self.error = error

    def initialize(self) -> None:
        raise HardwareInitError(self.error or "motor driver unavailable")

    def _apply(self, left: float, right: float) -> None:
        if left != 0.0 or right != 0.0:
            raise HardwareInitError(f"motor driver unavailable: {self.error}")
