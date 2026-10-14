"""Clocks.

Two notions of time exist in the system:

* **Real monotonic time** (``time.monotonic``) is used for every *safety* timeout:
  command leases, the dead-man, watchdogs and sensor staleness. It is never scaled.
* **Mission time** (a :class:`Clock`) drives physics, open-loop integration and
  mission timing. In simulation it can run faster than real time so long survey
  missions can be tested quickly; on hardware it is identical to real time.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Clock:
    """Mission clock interface (real time)."""

    scale: float = 1.0

    def now(self) -> float:
        return time.monotonic()


class ScaledClock(Clock):
    """Monotonic clock whose rate can be changed at runtime (simulation only)."""

    def __init__(self, scale: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._real0 = time.monotonic()
        self._sim0 = 0.0
        self.scale = float(scale)

    def now(self) -> float:
        with self._lock:
            return self._sim0 + (time.monotonic() - self._real0) * self.scale

    def set_scale(self, scale: float) -> None:
        with self._lock:
            real = time.monotonic()
            self._sim0 = self._sim0 + (real - self._real0) * self.scale
            self._real0 = real
            self.scale = float(scale)
