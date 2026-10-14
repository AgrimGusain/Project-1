"""Raspberry Pi 5 GPIO access via ``lgpio``.

Why lgpio: the Pi 5 routes its 40-pin header through the RP1 southbridge.
``RPi.GPIO`` does not support RP1; ``lgpio`` (the default backend of gpiozero on
Raspberry Pi OS Bookworm) uses the Linux GPIO character device and works on the
Pi 5. It is installed from apt (``python3-lgpio``); create the virtualenv with
``--system-site-packages`` so it is importable.

All GPIO access in the application goes through :class:`GpioChip`. Web routes,
database code and callbacks never touch pins directly.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class HardwareInitError(RuntimeError):
    """A real hardware device could not be initialised. Never silently replaced by simulation."""


def _import_lgpio() -> Any:
    try:
        import lgpio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on platform
        raise HardwareInitError(
            "lgpio is not installed. On Raspberry Pi OS: 'sudo apt install python3-lgpio' "
            "and create the venv with --system-site-packages."
        ) from exc
    return lgpio


PULL_FLAGS = {"up": "SET_PULL_UP", "down": "SET_PULL_DOWN", "none": "SET_PULL_NONE"}


class GpioChip:
    """Owns the gpiochip handle and tracks claimed lines so they are released on close."""

    def __init__(self, chip: int | str = "auto") -> None:
        self._requested = chip
        self._lg: Any = None
        self.handle: int | None = None
        self.chip_number: int | None = None
        self.label: str | None = None
        self._claimed: set[int] = set()
        self._lock = threading.RLock()

    @property
    def lg(self) -> Any:
        if self._lg is None:
            raise HardwareInitError("GPIO chip not opened")
        return self._lg

    def open(self) -> None:
        with self._lock:
            if self.handle is not None:
                return
            lg = _import_lgpio()
            candidates = [self._requested] if self._requested != "auto" else [0, 4, 1, 2, 3, 5]
            last_error: Exception | None = None
            for n in candidates:
                try:
                    h = lg.gpiochip_open(int(n))
                except Exception as exc:  # lgpio.error
                    last_error = exc
                    continue
                try:
                    info = lg.gpio_get_chip_info(h)
                    label = str(info[3]) if len(info) > 3 else ""
                except Exception:
                    label = ""
                if self._requested == "auto" and "rp1" not in label.lower() and "pinctrl" not in label.lower():
                    lg.gpiochip_close(h)
                    continue
                self._lg, self.handle, self.chip_number, self.label = lg, h, int(n), label
                log.info("opened gpiochip%s (%s)", n, label)
                return
            raise HardwareInitError(f"could not open a Raspberry Pi GPIO chip ({last_error or 'no RP1 chip found'})")

    def claim_output(self, pin: int, level: int = 0) -> None:
        with self._lock:
            self.lg.gpio_claim_output(self.handle, pin, level)
            self._claimed.add(pin)

    def claim_input(self, pin: int, pull: str = "none") -> None:
        with self._lock:
            flag = getattr(self.lg, PULL_FLAGS[pull])
            self.lg.gpio_claim_input(self.handle, pin, flag)
            self._claimed.add(pin)

    def claim_alert(self, pin: int, pull: str = "none") -> None:
        with self._lock:
            flag = getattr(self.lg, PULL_FLAGS[pull])
            self.lg.gpio_claim_alert(self.handle, pin, self.lg.BOTH_EDGES, flag)
            self._claimed.add(pin)

    def write(self, pin: int, level: int) -> None:
        self.lg.gpio_write(self.handle, pin, 1 if level else 0)

    def read(self, pin: int) -> int:
        return int(self.lg.gpio_read(self.handle, pin))

    def pwm(self, pin: int, frequency_hz: int, duty_percent: float) -> None:
        duty = max(0.0, min(100.0, duty_percent))
        self.lg.tx_pwm(self.handle, pin, frequency_hz, duty)

    def callback(self, pin: int, func: Any) -> Any:
        return self.lg.callback(self.handle, pin, self.lg.BOTH_EDGES, func)

    def free(self, pin: int) -> None:
        with self._lock:
            if pin in self._claimed and self.handle is not None:
                try:
                    self.lg.gpio_free(self.handle, pin)
                except Exception:  # pragma: no cover
                    log.exception("failed to free GPIO %s", pin)
                self._claimed.discard(pin)

    def close(self) -> None:
        with self._lock:
            if self.handle is None:
                return
            for pin in list(self._claimed):
                self.free(pin)
            try:
                self.lg.gpiochip_close(self.handle)
            finally:
                self.handle = None


class LazyGpio:
    """Opens the chip only when the first real device asks for it."""

    def __init__(self, chip: int | str) -> None:
        self._chip = GpioChip(chip)
        self.opened = False

    def get(self) -> GpioChip:
        if not self.opened:
            self._chip.open()
            self.opened = True
        return self._chip

    def close(self) -> None:
        if self.opened:
            self._chip.close()
            self.opened = False
