"""Optional physical emergency-stop *input*.

This lets the software react to an e-stop button, but it is NOT an independent
safety circuit: a real e-stop must also cut motor power in hardware (see
docs/SAFETY.md). A read failure is treated as "pressed" (fail-safe).
"""

from __future__ import annotations

from rover.config import EstopInputConfig
from rover.hal.gpio import LazyGpio


class GpioEstopInput:
    def __init__(self, cfg: EstopInputConfig, gpio: LazyGpio) -> None:
        self.cfg = cfg
        self.gpio = gpio
        self.error: str | None = None
        self._chip = None

    def initialize(self) -> None:
        chip = self.gpio.get()
        chip.claim_input(self.cfg.pin, self.cfg.pull)
        self._chip = chip

    def pressed(self) -> bool:
        if self._chip is None:
            self.error = "not initialised"
            return True
        try:
            level = self._chip.read(self.cfg.pin)
        except Exception as exc:
            self.error = str(exc)
            return True
        self.error = None
        return level == 0 if self.cfg.active_low else level == 1

    def close(self) -> None:
        if self._chip is not None:
            self._chip.free(self.cfg.pin)
            self._chip = None
