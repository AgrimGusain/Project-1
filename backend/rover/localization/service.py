"""Localization subsystem facade: provider selection plus diagnostic heading assessment."""

from __future__ import annotations

from typing import Any

from rover.config import RoverConfig
from rover.localization.base import LocalizationEstimate, LocalizationProvider, NoLocalizationProvider
from rover.localization.gnss import GpsNmeaProvider, SimulatedGpsProvider
from rover.localization.local import OdometryProvider, SimulatedLocalProvider
from rover.localization.magnetic import assess_heading
from rover.sensors.manager import SensorManager


def build_provider(cfg: RoverConfig, sensors: SensorManager, world: Any | None) -> LocalizationProvider:
    p = cfg.localization.provider
    if p == "none":
        return NoLocalizationProvider()
    if p == "simulated":
        if world is None:
            raise ValueError("simulated localization requires simulation mode")
        return SimulatedLocalProvider(world)
    if p == "simulated_gps":
        if world is None:
            raise ValueError("simulated GPS requires simulation mode")
        return SimulatedGpsProvider(cfg.localization.gps, world, cfg.simulation.geo_origin, cfg.simulation.gps_noise_m)
    if p == "gps_nmea":
        return GpsNmeaProvider(cfg.localization.gps)
    if p == "odometry":
        return OdometryProvider(sensors, cfg.calibration.effective_track_m)
    raise ValueError(f"unknown localization provider {p}")


class LocalizationService:
    def __init__(self, cfg: RoverConfig, provider: LocalizationProvider, sensors: SensorManager) -> None:
        self.cfg = cfg
        self.provider = provider
        self.sensors = sensors
        self.motors_active = lambda: False

    async def start(self) -> None:
        await self.provider.start()

    async def stop(self) -> None:
        await self.provider.stop()

    def estimate(self) -> LocalizationEstimate:
        return self.provider.estimate()

    def magnetic_heading(self) -> list[dict[str, Any]]:
        out = []
        for ch in self.sensors.of_type("magnetometer"):
            res = assess_heading(ch.usable_reading(), ch.cfg, motors_active=self.motors_active())
            res.update({"sensor_id": ch.id, "simulated": ch.simulated, "used_for_navigation": False})
            out.append(res)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider.describe(),
            "estimate": self.estimate().to_dict(),
            "magnetic_heading": self.magnetic_heading(),
            "fusion": {
                "implemented": ["gnss_position_with_cog_heading", "wheel_odometry_dead_reckoning(experimental)",
                                "odometry_with_imu_yaw_rate(experimental)"],
                "not_implemented": ["gnss_odometry_fusion", "imu_orientation_fusion", "magnetometer_heading_fusion"],
            },
        }
