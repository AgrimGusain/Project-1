"""Standard sensor interface.

Every device — ultrasonic, IR, metal detector, magnetometer, IMU, wheel encoder,
battery monitor, temperature, and anything added later — implements
:class:`Sensor`. The :class:`~rover.sensors.manager.SensorManager` owns the
lifecycle (initialisation, sampling rate, timeouts, reconnection, staleness), so
drivers only implement device-specific acquisition.

Readings are structured: ``value`` is a mapping of named fields, each with its
own unit in ``units`` (e.g. a magnetometer reports ``x``, ``y``, ``z`` in uT),
rather than forcing every device into a single scalar.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from rover.core.clock import iso, utcnow

if TYPE_CHECKING:
    from rover.sensors.drivers import DriverContext


class ConnectionState(StrEnum):
    """Device lifecycle, independent of the validity of any single reading."""

    DISABLED = "disabled"  # enabled: false in configuration
    CONFIGURED = "configured"  # enabled, not started yet
    INITIALIZING = "initializing"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"  # was connected / reachable, now failing; retrying
    FAULTY = "faulty"  # initialisation failed or driver misconfigured; retrying


class ReadingHealth(StrEnum):
    OK = "ok"
    INVALID = "invalid"  # device answered but measurement is not usable
    STALE = "stale"  # no new measurement within stale_after_s
    NONE = "none"  # never produced a reading


class SensorError(Exception):
    """Communication failure while reading a device (counts towards disconnection)."""


class SensorInitError(SensorError):
    """Device could not be initialised."""


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    sensor_type: str
    timestamp: datetime
    monotonic: float
    value: dict[str, Any]
    units: dict[str, str]
    valid: bool
    simulated: bool
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def field(self, name: str) -> Any:
        return self.value.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "sensor_type": self.sensor_type,
            "timestamp": iso(self.timestamp),
            "value": self.value,
            "units": self.units,
            "valid": self.valid,
            "simulated": self.simulated,
            "error": self.error,
            "metadata": self.metadata,
        }


class Sensor(ABC):
    """Base class for all sensor drivers.

    Subclasses set ``sensor_type`` and ``driver_name`` and implement :meth:`acquire`.
    ``acquire`` may block briefly; the manager calls it from a worker thread with a
    timeout. It returns ``None`` when no *new* measurement is available (which ages
    the last reading towards STALE) and raises :class:`SensorError` on I/O failure.
    """

    sensor_type: ClassVar[str] = "generic"
    driver_name: ClassVar[str] = "generic"
    simulated: ClassVar[bool] = False
    # True only once the driver has been verified against the physical device.
    physically_verified: ClassVar[bool] = False

    def __init__(self, config: Any, ctx: "DriverContext") -> None:
        self.config = config
        self.ctx = ctx

    @property
    def id(self) -> str:
        return self.config.id

    def initialize(self) -> None:
        """Open the device. Raise :class:`SensorInitError` on failure."""

    def close(self) -> None:
        """Release the device. Must be safe to call more than once."""

    @abstractmethod
    def acquire(self) -> SensorReading | None:
        ...

    def calibration(self) -> dict[str, Any]:
        return {"state": "not_applicable"}

    def describe(self) -> dict[str, Any]:
        return {"driver": self.driver_name, "simulated": self.simulated, "physically_verified": self.physically_verified}

    # helper for subclasses
    def reading(
        self,
        value: dict[str, Any],
        units: dict[str, str],
        *,
        valid: bool = True,
        error: str | None = None,
        **metadata: Any,
    ) -> SensorReading:
        return SensorReading(
            sensor_id=self.id,
            sensor_type=self.sensor_type,
            timestamp=utcnow(),
            monotonic=time.monotonic(),
            value=value,
            units=units,
            valid=valid,
            simulated=self.simulated,
            error=error,
            metadata=metadata,
        )
