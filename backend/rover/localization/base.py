"""Localization interfaces.

A :class:`LocalizationProvider` turns raw device data into a
:class:`LocalizationEstimate`. Position, heading, accuracy, timestamps and
validity are separate fields: a valid GPS position does NOT imply a valid heading.
Providers never invent a position when their source has none.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from rover.core.clock import iso, utcnow
from rover.core.enums import LocalizationStatus

EstimateKind = Literal["none", "local", "geodetic"]


@dataclass
class LocalizationEstimate:
    provider: str
    kind: EstimateKind
    simulated: bool
    status: LocalizationStatus
    message: str = ""
    position_valid: bool = False
    x: float | None = None  # local frame (grid or odometry start frame)
    y: float | None = None
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    horizontal_accuracy_m: float | None = None
    accuracy_source: str | None = None
    heading_valid: bool = False
    heading_deg: float | None = None
    heading_source: str | None = None
    heading_accuracy_deg: float | None = None
    speed_mps: float | None = None
    fix_quality: int | None = None
    satellites: int | None = None
    hdop: float | None = None
    timestamp: datetime | None = None
    monotonic: float | None = None
    experimental: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float | None:
        return None if self.monotonic is None else time.monotonic() - self.monotonic

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["timestamp"] = iso(self.timestamp)
        d.pop("monotonic", None)
        age = self.age_s
        d["age_s"] = None if age is None else round(age, 3)
        for k in ("x", "y", "heading_deg", "horizontal_accuracy_m", "speed_mps"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        return d


def unavailable(provider: str, message: str, status: LocalizationStatus = LocalizationStatus.UNAVAILABLE,
                simulated: bool = False, kind: EstimateKind = "none") -> LocalizationEstimate:
    return LocalizationEstimate(provider=provider, kind=kind, simulated=simulated, status=status, message=message,
                                timestamp=utcnow(), monotonic=time.monotonic())


class LocalizationProvider(ABC):
    name = "abstract"
    kind: EstimateKind = "none"
    simulated = False
    experimental = False
    # True when heading is available while stationary (e.g. simulated truth). GNSS
    # course-over-ground is only valid while moving.
    stationary_heading = False

    async def start(self) -> None:  # noqa: B027 - optional hook
        pass

    async def stop(self) -> None:  # noqa: B027
        pass

    @abstractmethod
    def estimate(self) -> LocalizationEstimate: ...

    def reset_local_pose(self, x: float, y: float, heading_deg: float) -> None:
        """Only meaningful for relative (dead-reckoning) providers."""

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name, "kind": self.kind, "simulated": self.simulated,
            "experimental": self.experimental, "stationary_heading": self.stationary_heading,
        }


class NoLocalizationProvider(LocalizationProvider):
    """Explicitly reports that physical position and heading are unavailable."""

    name = "none"
    kind = "none"

    def estimate(self) -> LocalizationEstimate:
        return unavailable(
            self.name,
            "No localization source is installed: physical position and heading are unavailable. "
            "Grid missions run open-loop (commanded motion only).",
        )
