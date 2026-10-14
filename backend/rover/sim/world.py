"""Deterministic simulated world used by every simulated device.

Coordinate convention (shared with the planner, see docs/COORDINATES.md):

* local frame in metres: ``x`` to the right/east, ``y`` forward/north;
* heading in degrees, compass style: 0 = +y, 90 = +x, clockwise positive;
* rover body frame: ``x`` right, ``y`` forward, ``z`` up.

The world's *true* motion model deliberately differs from the calibration model
(``speed_bias``/``turn_bias``/noise) so that open-loop execution drifts the way a
real chassis would. Ground truth is exposed only as simulation truth and is never
reported as a localization measurement unless the simulated localization provider
is selected.
"""

from __future__ import annotations

import asyncio
import math
import random
import threading
from dataclasses import dataclass

from rover.config import RoverConfig, SensorMount, SimObstacle
from rover.core.clock import Clock

SIM_FAULTS = {
    "ultrasonic_no_echo": "Ultrasonic sensors return no echo (invalid measurement)",
    "ultrasonic_stale": "Ultrasonic sensors stop producing new measurements",
    "ultrasonic_disconnect": "Ultrasonic sensors raise I/O errors (disconnected)",
    "ir_fault": "IR sensors produce invalid readings",
    "gps_loss": "Simulated GPS loses its fix",
    "gps_degraded": "Simulated GPS reports poor accuracy (HDOP 6)",
    "magnetometer_disconnect": "Magnetometer raises I/O errors",
    "motor_driver_fault": "Simulated motor driver stops responding (outputs ignored)",
    "camera_offline": "Simulated camera stops producing frames",
    "control_link_drop": "Incoming manual-control messages are dropped (tests dead-man)",
    "control_loop_stall": "Motion control loop stops updating the driver (tests driver watchdog)",
}

EARTH_FIELD_H_UT = 38.0
EARTH_FIELD_DOWN_UT = 30.0
SIM_HARD_IRON_UT = (12.0, -8.0, 4.0)
MOTOR_INTERFERENCE_UT = 9.0


@dataclass
class Pose:
    x: float
    y: float
    heading_deg: float

    def to_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "heading_deg": round(self.heading_deg % 360.0, 2)}


def _norm_deg(a: float) -> float:
    return a % 360.0


class SimWorld:
    def __init__(self, config: RoverConfig, clock: Clock) -> None:
        self.cfg = config
        self.sim = config.simulation
        self.calib = config.calibration
        self.clock = clock
        self._lock = threading.RLock()
        self._rng = random.Random(self.sim.seed)
        start = self.sim.start_pose
        self.pose = Pose(start.x, start.y, start.heading_deg)
        self.outputs = (0.0, 0.0)
        self.v_mps = 0.0
        self.yaw_rate_dps = 0.0
        self.wheel_distance = {"left": 0.0, "right": 0.0}
        self.collided = False
        self.faults: set[str] = set()
        self.battery_ah_used = 0.0
        self.sim_time = 0.0
        self.obstacles: list[SimObstacle] = list(self.sim.obstacles)
        self._last_t: float | None = None

    # ------------------------------------------------------------------ control

    def set_motor_outputs(self, left: float, right: float) -> None:
        with self._lock:
            if "motor_driver_fault" in self.faults:
                return
            self.outputs = (float(left), float(right))

    def inject_fault(self, name: str) -> None:
        if name not in SIM_FAULTS:
            raise ValueError(f"unknown simulated fault '{name}'")
        with self._lock:
            self.faults.add(name)
            if name == "motor_driver_fault":
                pass  # outputs freeze at their last value, like a hung driver

    def clear_fault(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self.faults.clear()
            else:
                self.faults.discard(name)

    def has_fault(self, name: str) -> bool:
        return name in self.faults

    def reset(self, x: float | None = None, y: float | None = None, heading_deg: float | None = None) -> None:
        with self._lock:
            start = self.sim.start_pose
            self.pose = Pose(
                start.x if x is None else x,
                start.y if y is None else y,
                start.heading_deg if heading_deg is None else heading_deg,
            )
            self.outputs = (0.0, 0.0)
            self.v_mps = 0.0
            self.yaw_rate_dps = 0.0
            self.collided = False
            self._rng = random.Random(self.sim.seed)

    # ------------------------------------------------------------------ physics

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        with self._lock:
            self.sim_time += dt
            left, right = self.outputs
            v_full = self.calib.full_speed_mps * (1.0 + self.sim.speed_bias)
            noise = self.sim.wheel_noise
            vl = left * v_full * (1.0 + self._rng.gauss(0.0, noise) if left else 0.0)
            vr = right * v_full * (1.0 + self._rng.gauss(0.0, noise) if right else 0.0)
            v = (vl + vr) / 2.0
            omega = (vl - vr) / self.calib.effective_track_m * (1.0 + self.sim.turn_bias)
            h = math.radians(self.pose.heading_deg)
            h_mid = h + omega * dt / 2.0
            nx = self.pose.x + v * math.sin(h_mid) * dt
            ny = self.pose.y + v * math.cos(h_mid) * dt
            if self._inside_obstacle(nx, ny, margin=0.12):
                self.collided = True
                v = 0.0
                nx, ny = self.pose.x, self.pose.y
            else:
                self.collided = False
            self.pose = Pose(nx, ny, _norm_deg(math.degrees(h + omega * dt)))
            self.v_mps = v
            self.yaw_rate_dps = math.degrees(omega)
            self.wheel_distance["left"] += vl * dt
            self.wheel_distance["right"] += vr * dt
            current = 0.25 + 1.6 * (abs(left) + abs(right)) / 2.0
            self.battery_ah_used += current * dt / 3600.0

    async def run(self) -> None:
        period = 1.0 / self.sim.physics_rate_hz
        self._last_t = self.clock.now()
        while True:
            await asyncio.sleep(period)
            now = self.clock.now()
            dt = now - (self._last_t or now)
            self._last_t = now
            # Integrate in bounded sub-steps so a high speed factor stays stable.
            while dt > 1e-6:
                step = min(dt, 0.02)
                self.step(step)
                dt -= step

    # ------------------------------------------------------------------ geometry

    def _inside_obstacle(self, x: float, y: float, margin: float = 0.0) -> bool:
        for ob in self.obstacles:
            if ob.type == "circle":
                if math.hypot(x - ob.x, y - ob.y) <= (ob.r or 0) + margin:
                    return True
            else:
                if abs(x - ob.x) <= (ob.w or 0) / 2 + margin and abs(y - ob.y) <= (ob.h or 0) / 2 + margin:
                    return True
        return False

    def sensor_pose(self, mount: SensorMount) -> tuple[float, float, float]:
        with self._lock:
            p = self.pose
        h = math.radians(p.heading_deg)
        # body (x right, y forward) -> world
        wx = p.x + mount.x_m * math.cos(h) + mount.y_m * math.sin(h)
        wy = p.y - mount.x_m * math.sin(h) + mount.y_m * math.cos(h)
        return wx, wy, p.heading_deg + mount.yaw_deg

    @staticmethod
    def _ray_circle(ox: float, oy: float, dx: float, dy: float, cx: float, cy: float, r: float) -> float | None:
        fx, fy = ox - cx, oy - cy
        b = fx * dx + fy * dy
        c = fx * fx + fy * fy - r * r
        disc = b * b - c
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        for t in (-b - sq, -b + sq):
            if t >= 0:
                return t
        return None

    @staticmethod
    def _ray_rect(ox: float, oy: float, dx: float, dy: float, ob: SimObstacle) -> float | None:
        x0, x1 = ob.x - (ob.w or 0) / 2, ob.x + (ob.w or 0) / 2
        y0, y1 = ob.y - (ob.h or 0) / 2, ob.y + (ob.h or 0) / 2
        tmin, tmax = -math.inf, math.inf
        for o, d, lo, hi in ((ox, dx, x0, x1), (oy, dy, y0, y1)):
            if abs(d) < 1e-12:
                if o < lo or o > hi:
                    return None
            else:
                t1, t2 = (lo - o) / d, (hi - o) / d
                tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
        if tmax < max(tmin, 0.0):
            return None
        return tmin if tmin >= 0 else tmax

    def ray_distance(self, mount: SensorMount, half_angle_deg: float, max_range: float, rays: int = 5) -> float | None:
        ox, oy, heading = self.sensor_pose(mount)
        best: float | None = None
        for i in range(rays):
            frac = 0.0 if rays == 1 else i / (rays - 1) * 2 - 1
            a = math.radians(heading + frac * half_angle_deg)
            dx, dy = math.sin(a), math.cos(a)
            for ob in self.obstacles:
                if ob.type == "circle":
                    t = self._ray_circle(ox, oy, dx, dy, ob.x, ob.y, ob.r or 0)
                else:
                    t = self._ray_rect(ox, oy, dx, dy, ob)
                if t is not None and t <= max_range and (best is None or t < best):
                    best = t
        return best

    def gauss(self, sigma: float) -> float:
        with self._lock:
            return self._rng.gauss(0.0, sigma) if sigma > 0 else 0.0

    # ------------------------------------------------------------------ device models

    def metal_signal(self, mount: SensorMount) -> float:
        sx, sy, _ = self.sensor_pose(mount)
        total = 0.0
        for t in self.sim.metal_targets:
            d = math.hypot(sx - t.x, sy - t.y)
            total += t.strength * math.exp(-((d / t.radius_m) ** 2))
        return max(0.0, total + self.gauss(0.02))

    def magnetic_field_body(self) -> tuple[float, float, float]:
        """Body-frame field (uT): Earth field + fixed hard-iron offset + motor interference."""
        with self._lock:
            h = math.radians(self.pose.heading_deg)
            left, right = self.outputs
        bx = -EARTH_FIELD_H_UT * math.sin(h)
        by = EARTH_FIELD_H_UT * math.cos(h)
        bz = -EARTH_FIELD_DOWN_UT
        load = (abs(left) + abs(right)) / 2.0
        bx += SIM_HARD_IRON_UT[0] + MOTOR_INTERFERENCE_UT * load
        by += SIM_HARD_IRON_UT[1]
        bz += SIM_HARD_IRON_UT[2] + 0.5 * MOTOR_INTERFERENCE_UT * load
        return (bx + self.gauss(0.3), by + self.gauss(0.3), bz + self.gauss(0.3))

    def imu(self) -> dict[str, tuple[float, float, float]]:
        with self._lock:
            yaw_dps = self.yaw_rate_dps
        # z-up right-handed: clockwise heading change is a negative z rotation.
        gyro = (self.gauss(0.05), self.gauss(0.05), -yaw_dps + self.gauss(0.1))
        accel = (self.gauss(0.05), self.gauss(0.05), 9.81 + self.gauss(0.05))
        return {"gyro_dps": gyro, "accel_mps2": accel}

    def wheel_travel(self, side: str) -> float:
        with self._lock:
            return self.wheel_distance[side]

    def battery(self, cells: int) -> tuple[float, float]:
        with self._lock:
            used = self.battery_ah_used
            left, right = self.outputs
        capacity_ah = 2.5
        soc = max(0.0, 1.0 - used / capacity_ah)
        current = 0.25 + 1.6 * (abs(left) + abs(right)) / 2.0
        cell_v = 3.5 + 0.65 * soc - 0.05 * current
        return cells * cell_v + self.gauss(0.01), current + self.gauss(0.01)

    def truth(self) -> dict[str, object]:
        with self._lock:
            return {
                "pose": self.pose.to_dict(),
                "speed_mps": round(self.v_mps, 3),
                "yaw_rate_dps": round(self.yaw_rate_dps, 2),
                "collided": self.collided,
                "sim_time_s": round(self.sim_time, 2),
            }
