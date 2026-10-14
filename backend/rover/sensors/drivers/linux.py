"""Sensors read from the Linux kernel (no extra hardware)."""

from __future__ import annotations

from pathlib import Path

from rover.sensors.base import Sensor, SensorError, SensorInitError, SensorReading
from rover.sensors.drivers import register


@register
class LinuxThermalZone(Sensor):
    """Reads /sys/class/thermal/thermal_zoneN/temp (millidegrees C), e.g. the Pi 5 SoC."""

    sensor_type = "temperature"
    driver_name = "linux_thermal"
    simulated = False
    physically_verified = False

    @property
    def path(self) -> Path:
        return Path(f"/sys/class/thermal/thermal_zone{self.config.thermal_zone}/temp")

    def initialize(self) -> None:
        if not self.path.exists():
            raise SensorInitError(f"{self.path} does not exist on this host")

    def acquire(self) -> SensorReading | None:
        try:
            raw = self.path.read_text().strip()
            temp_c = int(raw) / 1000.0
        except (OSError, ValueError) as exc:
            raise SensorError(f"thermal read failed: {exc}") from exc
        valid = -40.0 <= temp_c <= 125.0
        return self.reading(
            {"temperature": round(temp_c, 2)},
            {"temperature": "degC"},
            valid=valid,
            error=None if valid else "implausible_value",
        )
