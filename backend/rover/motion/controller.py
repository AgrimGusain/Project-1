"""Motion controller: the single authoritative owner of motor outputs.

* Exactly one *owner* (manual session or mission executor) may command motion at a
  time. Commands from anyone else are rejected.
* Every command is a **lease** with a short real-time expiry. If the owner stops
  renewing it (dead-man released, browser gone, executor hung), the controller
  outputs zero on the next tick. Lease timing uses real monotonic time, never the
  scaled simulation clock.
* Outputs are limited (max output), rate-limited (accel/decel), and pass through a
  direction-reversal interlock (ramp to zero and dwell before reversing).
* Forward/reverse motion is blocked when the obstacle assessment says so.
* An emergency halt zeroes the outputs immediately (no ramp), drops the lease and
  increments the command epoch so any in-flight command is invalid.
* The driver itself is wrapped in a thread watchdog (see hal.motors).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rover.config import RoverConfig
from rover.core.clock import Clock
from rover.core.enums import ControlOwner, Severity
from rover.core.events import EventLog, FaultManager
from rover.hal.motors import MotorDriver
from rover.safety.obstacles import ObstacleAssessment

log = logging.getLogger(__name__)


class CommandRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Lease:
    owner: ControlOwner
    holder_id: str
    left: float
    right: float
    issued_at: float
    expires_at: float
    epoch: int


def _sign(v: float) -> int:
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


class MotionController:
    def __init__(
        self,
        driver: MotorDriver,
        config: RoverConfig,
        clock: Clock,
        events: EventLog,
        faults: FaultManager,
    ) -> None:
        self.driver = driver
        self.cfg = config.motion
        self.safety_cfg = config.safety
        self.clock = clock
        self.events = events
        self.faults = faults
        # Wired by the runtime:
        self.permit: Callable[[], tuple[bool, str | None]] = lambda: (False, "safety supervisor not connected")
        self.obstacles: Callable[[], ObstacleAssessment | None] = lambda: None
        self.stalled: Callable[[], bool] = lambda: False
        self.on_lease_expired: list[Callable[[ControlOwner, str], None]] = []

        self.owner = ControlOwner.NONE
        self.holder_id: str | None = None
        self.lease: Lease | None = None
        self.epoch = 0
        self.applied = [0.0, 0.0]
        self.target = (0.0, 0.0)
        self.requested = (0.0, 0.0)
        self._last_dir = [0, 0]
        self._zero_since = [clock.now(), clock.now()]
        self.blocked_reason: str | None = None
        self.limited_reason: str | None = None
        self.interlock_active = False
        self.last_tick_real: float | None = None
        self._last_tick_clock: float | None = clock.now()
        self.last_motion_real = time.monotonic()
        self.driver_error: str | None = None
        self._running = False

    # ------------------------------------------------------------------ ownership

    def acquire(self, owner: ControlOwner, holder_id: str) -> None:
        if owner == ControlOwner.NONE:
            raise ValueError("cannot acquire as NONE")
        if self.owner not in (ControlOwner.NONE, owner) or (self.owner == owner and self.holder_id not in (None, holder_id)):
            raise CommandRejected("controller_busy", f"motion is owned by {self.owner.value} ({self.holder_id})")
        self.owner = owner
        self.holder_id = holder_id

    def release(self, owner: ControlOwner, holder_id: str) -> None:
        if self.owner == owner and self.holder_id == holder_id:
            self.owner = ControlOwner.NONE
            self.holder_id = None
            self.lease = None

    def is_owner(self, owner: ControlOwner, holder_id: str) -> bool:
        return self.owner == owner and self.holder_id == holder_id

    # ------------------------------------------------------------------ commands

    def command(self, owner: ControlOwner, holder_id: str, left: float, right: float, ttl_s: float) -> Lease:
        for v in (left, right):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or abs(v) > 1.0:
                raise CommandRejected("invalid_command", f"motor command out of range: {v!r}")
        if not (0 < ttl_s <= 2.0):
            raise CommandRejected("invalid_command", "ttl must be within (0, 2] s")
        if not self.is_owner(owner, holder_id):
            raise CommandRejected("not_owner", f"motion is owned by {self.owner.value}")
        if left != 0.0 or right != 0.0:
            ok, why = self.permit()
            if not ok:
                raise CommandRejected("motion_not_permitted", why or "motion not permitted")
        now = time.monotonic()
        self.lease = Lease(owner, holder_id, float(left), float(right), now, now + ttl_s, self.epoch)
        return self.lease

    def zero(self, owner: ControlOwner, holder_id: str) -> None:
        if self.is_owner(owner, holder_id):
            now = time.monotonic()
            self.lease = Lease(owner, holder_id, 0.0, 0.0, now, now + 0.5, self.epoch)

    def emergency_halt(self, reason: str) -> None:
        """Immediate zero output: no ramp, lease dropped, epoch advanced."""
        self.epoch += 1
        self.lease = None
        self.target = (0.0, 0.0)
        self.requested = (0.0, 0.0)
        self.applied = [0.0, 0.0]
        now = self.clock.now()
        self._zero_since = [now, now]
        try:
            self.driver.stop()
        except Exception as exc:  # pragma: no cover - hardware failure path
            log.exception("driver stop failed during emergency halt")
            self.driver_error = str(exc)
        log.warning("emergency halt: %s", reason)

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        self._running = True
        try:
            while True:
                self.tick()
                # In accelerated simulation the loop runs faster so ramps stay faithful.
                await asyncio.sleep(max(0.004, 1.0 / (self.cfg.control_rate_hz * max(self.clock.scale, 1.0))))
        finally:
            self._running = False
            self.emergency_halt("control loop stopped")

    @property
    def loop_healthy(self) -> bool:
        if self.last_tick_real is None:
            return False
        return time.monotonic() - self.last_tick_real < max(0.25, 5.0 / self.cfg.control_rate_hz)

    def tick(self) -> None:
        if self.stalled():
            return  # simulated control-loop stall: the driver watchdog must catch this
        now_real = time.monotonic()
        now = self.clock.now()
        # Cap dt so a delayed tick can never produce a large ramp step.
        dt = 0.0 if self._last_tick_clock is None else min(0.1, max(0.0, now - self._last_tick_clock))
        self._last_tick_clock = now
        self.last_tick_real = now_real

        left = right = 0.0
        lease = self.lease
        if lease is not None:
            if lease.epoch != self.epoch:
                self.lease = None
            elif now_real <= lease.expires_at:
                left, right = lease.left, lease.right
            else:
                self.lease = None
                if lease.left != 0.0 or lease.right != 0.0:
                    self._lease_expired(lease)
        self.requested = (left, right)

        permitted, why = self.permit()
        if not permitted:
            if self.applied != [0.0, 0.0]:
                self.events.emit(Severity.WARNING, "motion", "motion.halted", f"Motion stopped: {why}")
            self.applied = [0.0, 0.0]
            self.target = (0.0, 0.0)
            self._zero_since = [now, now]
            self._write(0.0, 0.0)
            return

        left, right = self._limit(left, right)
        self.target = (left, right)
        self._ramp(dt, now)
        self._write(self.applied[0], self.applied[1])
        if self.applied != [0.0, 0.0]:
            self.last_motion_real = now_real

    def _lease_expired(self, lease: Lease) -> None:
        age = time.monotonic() - lease.issued_at
        if lease.owner == ControlOwner.MANUAL:
            msg = f"Dead-man timeout: no manual control update for {age:.2f} s, motors stopped"
            level = Severity.WARNING
        else:
            msg = f"Mission command lease expired after {age:.2f} s, motors stopped"
            level = Severity.ERROR
        self.events.emit(level, "motion", "motion.lease_expired", msg, owner=lease.owner.value)
        for cb in self.on_lease_expired:
            try:
                cb(lease.owner, lease.holder_id)
            except Exception:  # pragma: no cover
                log.exception("lease-expired callback failed")

    def _limit(self, left: float, right: float) -> tuple[float, float]:
        cap = self.cfg.max_output
        left, right = max(-cap, min(cap, left)), max(-cap, min(cap, right))
        self.blocked_reason = None
        self.limited_reason = None
        assessment = self.obstacles()
        if assessment is None:
            return left, right
        forward = (left + right) / 2.0
        if forward > 0.02 and assessment.block_forward:
            self.blocked_reason = "forward blocked: " + ("; ".join(assessment.front.reasons) or assessment.front.state.value)
            return 0.0, 0.0
        if forward < -0.02 and assessment.block_reverse:
            self.blocked_reason = "reverse blocked: " + ("; ".join(assessment.rear.reasons) or assessment.rear.state.value)
            return 0.0, 0.0
        if forward > 0.02 and assessment.front.slow:
            slow = self.cfg.obstacle_slow_output
            peak = max(abs(left), abs(right))
            if peak > slow:
                k = slow / peak
                left, right = left * k, right * k
                self.limited_reason = "slowed: obstacle within slow-down distance"
        return left, right

    def _ramp(self, dt: float, now: float) -> None:
        self.interlock_active = False
        for i, t in enumerate(self.target):
            a = self.applied[i]
            if _sign(a) != 0 and _sign(t) != 0 and _sign(t) != _sign(a):
                t = 0.0  # decelerate to zero before any reversal
                self.interlock_active = True
            if (
                _sign(a) == 0
                and _sign(t) != 0
                and self._last_dir[i] != 0
                and _sign(t) != self._last_dir[i]
                and now - self._zero_since[i] < self.cfg.reversal_dwell_s
            ):
                t = 0.0
                self.interlock_active = True
            increasing = abs(t) > abs(a) and (_sign(a) in (0, _sign(t)))
            rate = self.cfg.max_accel_per_s if increasing else self.cfg.max_decel_per_s
            step = rate * dt
            if abs(t - a) <= step:
                new = t
            else:
                new = a + step * (1 if t > a else -1)
            if abs(new) < 1e-4:
                new = 0.0
            if new == 0.0 and a != 0.0:
                self._zero_since[i] = now
            if new != 0.0:
                self._last_dir[i] = _sign(new)
            self.applied[i] = new

    def _write(self, left: float, right: float) -> None:
        try:
            self.driver.set_outputs(left, right)
            if self.driver_error:
                self.driver_error = None
                self.faults.clear("motor_driver_error", by="recovered")
        except Exception as exc:
            if self.driver_error is None:
                self.driver_error = str(exc)
                self.faults.raise_fault("motor_driver_error", Severity.CRITICAL, "motion", f"Motor driver error: {exc}")
            self.applied = [0.0, 0.0]

    def snapshot(self) -> dict[str, Any]:
        lease = self.lease
        return {
            "owner": self.owner.value,
            "holder_id": self.holder_id,
            "requested": {"left": round(self.requested[0], 3), "right": round(self.requested[1], 3)},
            "target": {"left": round(self.target[0], 3), "right": round(self.target[1], 3)},
            "applied": {"left": round(self.applied[0], 3), "right": round(self.applied[1], 3)},
            "lease_remaining_s": None if lease is None else round(max(0.0, lease.expires_at - time.monotonic()), 3),
            "blocked_reason": self.blocked_reason,
            "limited_reason": self.limited_reason,
            "interlock_active": self.interlock_active,
            "control_loop_ok": self.loop_healthy,
            "max_output": self.cfg.max_output,
            "epoch": self.epoch,
            "driver": self.driver.status(),
        }
