"""Local-frame providers: simulated truth and wheel-encoder dead reckoning."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from rover.core.clock import utcnow
from rover.core.enums import LocalizationStatus
from rover.localization.base import LocalizationEstimate, LocalizationProvider, unavailable
from rover.sensors.base import SensorReading


class SimulatedLocalProvider(LocalizationProvider):
    """Ground-truth pose from the simulator (plus small noise). Simulation only."""

    name = "simulated"
    kind = "local"
    simulated = True
    stationary_heading = True

    def __init__(self, world: Any, noise_m: float = 0.005, noise_deg: float = 0.3) -> None:
        self.world = world
        self.noise_m = noise_m
        self.noise_deg = noise_deg

    def estimate(self) -> LocalizationEstimate:
        t = self.world.truth()
        p = t["pose"]
        return LocalizationEstimate(
            provider=self.name, kind="local", simulated=True, status=LocalizationStatus.OK,
            message="simulated ground truth", position_valid=True,
            x=p["x"] + self.world.gauss(self.noise_m), y=p["y"] + self.world.gauss(self.noise_m),
            horizontal_accuracy_m=self.noise_m * 2, accuracy_source="simulation",
            heading_valid=True, heading_deg=(p["heading_deg"] + self.world.gauss(self.noise_deg)) % 360.0,
            heading_source="simulation", heading_accuracy_deg=self.noise_deg * 2, speed_mps=t["speed_mps"],
            timestamp=utcnow(), monotonic=time.monotonic(),
        )


class OdometryProvider(LocalizationProvider):
    """EXPERIMENTAL differential-drive dead reckoning from left/right wheel encoders.

    Implemented and tested against simulated encoders only. Skid-steer wheels slip,
    so position error grows with distance travelled; the reported accuracy is a
    coarse ``drift_ratio * distance`` bound, not a validated uncertainty. Position is
    relative to the pose given to :meth:`reset_local_pose` (the mission start pose).
    Optional IMU yaw-rate integration replaces encoder-derived heading changes.
    """

    name = "odometry"
    kind = "local"
    experimental = True
    stationary_heading = True

    def __init__(self, sensors: Any, track_m: float, stale_s: float = 0.5, drift_ratio: float = 0.05) -> None:
        self.sensors = sensors
        self.track_m = track_m
        self.stale_s = stale_s
        self.drift_ratio = drift_ratio
        self._lock = threading.Lock()
        self.x = self.y = self.heading = 0.0
        self.distance = 0.0
        self.initialized = False
        self._last_counts: dict[str, int] = {}
        self._last_update: dict[str, float] = {}
        self._pending: dict[str, float] = {"left": 0.0, "right": 0.0}
        self._imu_rate_dps: float | None = None
        self._imu_mono: float | None = None
        self._enc_cfg: dict[str, Any] = {}
        self.use_imu = False
        for ch in sensors.of_type("wheel_encoder"):
            self._enc_cfg[ch.cfg.side] = ch.cfg
        self.simulated = any(ch.simulated for ch in sensors.of_type("wheel_encoder"))
        self.use_imu = bool(sensors.of_type("imu"))
        sensors.add_listener(self.on_reading)

    def reset_local_pose(self, x: float, y: float, heading_deg: float) -> None:
        with self._lock:
            self.x, self.y, self.heading = x, y, heading_deg % 360.0
            self.distance = 0.0
            self.initialized = True
            self._pending = {"left": 0.0, "right": 0.0}

    def on_reading(self, r: SensorReading) -> None:
        if not r.valid:
            return
        if r.sensor_type == "imu":
            self._imu_rate_dps = -float(r.value["gyro_z"])  # z-up gyro -> clockwise heading rate
            self._imu_mono = r.monotonic
            return
        if r.sensor_type != "wheel_encoder":
            return
        side = r.metadata.get("side")
        cfg = self._enc_cfg.get(side)
        if cfg is None:
            return
        counts = int(r.value["counts"])
        with self._lock:
            prev = self._last_counts.get(side)
            prev_t = self._last_update.get(side, r.monotonic)
            self._last_counts[side] = counts
            self._last_update[side] = r.monotonic
            if prev is None:
                return
            self._pending[side] += (counts - prev) / cfg.counts_per_rev * math.pi * cfg.wheel_diameter_m
            if side == "right":
                self._integrate(r.monotonic - prev_t)

    def _integrate(self, dt: float) -> None:
        dl, dr = self._pending["left"], self._pending["right"]
        self._pending = {"left": 0.0, "right": 0.0}
        dc = (dl + dr) / 2.0
        if self.use_imu and self._imu_rate_dps is not None and self._imu_mono and time.monotonic() - self._imu_mono < 0.3:
            dtheta = self._imu_rate_dps * max(dt, 0.0)
        else:
            dtheta = math.degrees((dl - dr) / self.track_m)
        mid = math.radians(self.heading + dtheta / 2.0)
        self.x += dc * math.sin(mid)
        self.y += dc * math.cos(mid)
        self.heading = (self.heading + dtheta) % 360.0
        self.distance += abs(dc)

    def estimate(self) -> LocalizationEstimate:
        now = time.monotonic()
        stale = [s for s in ("left", "right") if now - self._last_update.get(s, -1e9) > self.stale_s]
        if stale:
            return unavailable(self.name, f"wheel encoder data missing or stale: {', '.join(stale)}",
                               LocalizationStatus.DEGRADED, simulated=self.simulated, kind="local")
        if not self.initialized:
            return unavailable(self.name, "odometry not initialised (set at mission start)", LocalizationStatus.STARTING,
                               simulated=self.simulated, kind="local")
        with self._lock:
            return LocalizationEstimate(
                provider=self.name, kind="local", simulated=self.simulated, status=LocalizationStatus.OK,
                message="dead reckoning relative to mission start pose (experimental)", position_valid=True,
                x=self.x, y=self.y, horizontal_accuracy_m=0.02 + self.drift_ratio * self.distance,
                accuracy_source="drift_model_estimate", heading_valid=True, heading_deg=self.heading,
                heading_source="imu_gyro" if self.use_imu else "wheel_odometry",
                timestamp=utcnow(), monotonic=now, experimental=True,
            )
