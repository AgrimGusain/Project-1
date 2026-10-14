"""Safety supervisor: arming, preflight checks, emergency-stop latch, auto-disarm.

The rover always starts DISARMED with the e-stop latch clear. Movement requires:
hardware initialised, control loop healthy, e-stop clear (and the physical e-stop
input released if fitted), no arming-blocking faults, required sensors healthy,
and an explicit operator ``arm`` request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from rover.config import RoverConfig
from rover.core.clock import iso, utcnow
from rover.core.enums import Severity
from rover.core.events import EventLog, FaultManager
from rover.hal.estop import GpioEstopInput
from rover.motion.controller import MotionController
from rover.safety.obstacles import ObstacleAssessment, assess_obstacles
from rover.sensors.base import ConnectionState, ReadingHealth
from rover.sensors.manager import SensorManager

log = logging.getLogger(__name__)


@dataclass
class PreflightCheck:
    id: str
    label: str
    passed: bool
    required: bool
    message: str = ""


class ArmingRefused(Exception):
    def __init__(self, checks: list[PreflightCheck]) -> None:
        failed = [c for c in checks if c.required and not c.passed]
        super().__init__("; ".join(f"{c.label}: {c.message}" for c in failed) or "arming refused")
        self.checks = checks


class SafetySupervisor:
    def __init__(
        self,
        config: RoverConfig,
        controller: MotionController,
        sensors: SensorManager,
        faults: FaultManager,
        events: EventLog,
        estop_input: GpioEstopInput | None = None,
    ) -> None:
        self.config = config
        self.controller = controller
        self.sensors = sensors
        self.faults = faults
        self.events = events
        self.estop_input = estop_input
        self.estop_input_error: str | None = None
        self.armed = False
        self.armed_at: datetime | None = None
        self.armed_by: str | None = None
        self.estop_latched = False
        self.estop_reason: str | None = None
        self.estop_source: str | None = None
        self.estop_at: datetime | None = None
        self.estop_input_pressed: bool | None = None
        self.assessment: ObstacleAssessment | None = None
        self.on_emergency_stop: list[Callable[[str], None]] = []
        self.on_estop_reset: list[Callable[[], None]] = []
        self.on_disarm: list[Callable[[str], None]] = []
        controller.permit = self.motion_permitted
        controller.obstacles = lambda: self.assessment

    # ------------------------------------------------------------------ status

    def motion_permitted(self) -> tuple[bool, str | None]:
        if self.estop_latched:
            return False, f"emergency stop latched ({self.estop_reason})"
        if not self.armed:
            return False, "rover is disarmed"
        critical = self.faults.blocking_motion()
        if critical:
            return False, f"critical fault: {critical[0].message}"
        return True, None

    def fresh_assessment(self) -> ObstacleAssessment:
        """Recompute now (used for operator decisions such as start/resume)."""
        self.assessment = assess_obstacles(self.sensors, self.config.safety)
        return self.assessment

    def preflight(self) -> list[PreflightCheck]:
        checks: list[PreflightCheck] = []
        drv = self.controller.driver.status()
        checks.append(
            PreflightCheck(
                "motor_driver",
                "Motor driver initialised",
                bool(drv.get("healthy")),
                True,
                drv.get("error") or ("simulated driver" if drv.get("simulated") else drv.get("driver", "")),
            )
        )
        checks.append(
            PreflightCheck(
                "control_loop", "Motion control loop running", self.controller.loop_healthy, True,
                "" if self.controller.loop_healthy else "control loop not ticking",
            )
        )
        checks.append(
            PreflightCheck(
                "estop_clear", "Emergency-stop latch clear", not self.estop_latched, True,
                self.estop_reason or "" if self.estop_latched else "",
            )
        )
        if self.estop_input is not None:
            released = self.estop_input_pressed is False
            checks.append(
                PreflightCheck(
                    "estop_input", "Physical e-stop released", released, True,
                    "" if released else (self.estop_input_error or "e-stop input pressed or unreadable"),
                )
            )
        blocking = self.faults.blocking_arming()
        checks.append(
            PreflightCheck(
                "faults", "No blocking faults", not blocking, True,
                "; ".join(f.message for f in blocking[:3]),
            )
        )
        now = time.monotonic()
        for sid in self.config.safety.required_for_arming:
            ch = self.sensors.get(sid)
            ok = ch is not None and ch.state == ConnectionState.CONNECTED and ch.reading_health(now) == ReadingHealth.OK
            msg = ""
            if ch is None:
                msg = "not configured"
            elif not ok:
                msg = f"{ch.state.value}, reading {ch.reading_health(now).value}" + (f" ({ch.error})" if ch.error else "")
            checks.append(PreflightCheck(f"sensor_{sid}", f"Sensor '{sid}' healthy", ok, True, msg))
        if self.config.is_simulation:
            checks.append(
                PreflightCheck("simulation", "Simulation mode", True, False, "Simulated arming: no physical motors will move")
            )
        elif not self.config.calibration.calibrated:
            checks.append(
                PreflightCheck(
                    "calibration", "Open-loop calibration measured", False, False,
                    "calibration.calibrated is false; open-loop distances are unverified",
                )
            )
        return checks

    # ------------------------------------------------------------------ commands

    def arm(self, by: str = "operator") -> list[PreflightCheck]:
        checks = self.preflight()
        if any(c.required and not c.passed for c in checks):
            self.events.emit(Severity.WARNING, "safety", "safety.arm_refused", f"Arming refused: {ArmingRefused(checks)}")
            raise ArmingRefused(checks)
        if not self.armed:
            self.armed = True
            self.armed_at = utcnow()
            self.armed_by = by
            self.controller.last_motion_real = time.monotonic()
            label = "SIMULATED rover armed" if self.config.is_simulation else "Rover ARMED - motors may move"
            self.events.emit(Severity.WARNING, "safety", "safety.armed", f"{label} (by {by})")
        return checks

    def disarm(self, reason: str = "operator request", by: str = "operator") -> None:
        if not self.armed:
            return
        self.armed = False
        self.armed_at = None
        self.armed_by = None
        self.controller.emergency_halt(f"disarmed: {reason}")
        self.events.emit(Severity.INFO, "safety", "safety.disarmed", f"Rover disarmed ({reason}, by {by})")
        for cb in self.on_disarm:
            try:
                cb(reason)
            except Exception:  # pragma: no cover
                log.exception("disarm callback failed")

    def emergency_stop(self, source: str, reason: str) -> None:
        # Stop the motors first, before any bookkeeping.
        self.controller.emergency_halt(f"e-stop: {reason}")
        first = not self.estop_latched
        self.estop_latched = True
        if first:
            self.estop_reason = reason
            self.estop_source = source
            self.estop_at = utcnow()
        was_armed = self.armed
        self.armed = False
        self.armed_at = None
        if first:
            self.events.emit(
                Severity.CRITICAL, "safety", "safety.emergency_stop",
                f"EMERGENCY STOP ({source}): {reason}", was_armed=was_armed,
            )
            for cb in self.on_emergency_stop:
                try:
                    cb(reason)
                except Exception:  # pragma: no cover
                    log.exception("e-stop callback failed")

    def reset_emergency_stop(self, by: str = "operator") -> None:
        if not self.estop_latched:
            return
        if self.estop_input is not None and self.estop_input_pressed is not False:
            raise ArmingRefused(
                [PreflightCheck("estop_input", "Physical e-stop released", False, True, "release the physical e-stop first")]
            )
        self.controller.emergency_halt("e-stop reset (outputs held at zero)")
        self.estop_latched = False
        self.events.emit(
            Severity.WARNING, "safety", "safety.estop_reset",
            f"Emergency stop reset by {by}; rover remains DISARMED (was: {self.estop_reason})",
        )
        self.estop_reason = None
        self.estop_source = None
        self.estop_at = None
        for cb in self.on_estop_reset:
            try:
                cb()
            except Exception:  # pragma: no cover
                log.exception("e-stop reset callback failed")

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        period = 0.04
        while True:
            try:
                self.step()
            except Exception:  # pragma: no cover - supervisor must keep running
                log.exception("safety supervisor step failed")
            await asyncio.sleep(period)

    def step(self) -> None:
        self.assessment = assess_obstacles(self.sensors, self.config.safety)
        if self.estop_input is not None:
            pressed = self.estop_input.pressed()
            self.estop_input_error = self.estop_input.error
            self.estop_input_pressed = pressed
            if pressed:
                self.emergency_stop("physical_input", self.estop_input_error or "physical e-stop pressed")
        idle = self.config.safety.auto_disarm_idle_s
        if (
            self.armed
            and idle > 0
            and self.controller.owner.value != "mission"
            and time.monotonic() - self.controller.last_motion_real > idle
        ):
            self.disarm(f"idle for {idle:.0f} s", by="system")

    def snapshot(self) -> dict[str, Any]:
        permitted, why = self.motion_permitted()
        return {
            "armed": self.armed,
            "armed_at": iso(self.armed_at),
            "armed_by": self.armed_by,
            "simulated_arming": self.config.is_simulation,
            "estop_latched": self.estop_latched,
            "estop_reason": self.estop_reason,
            "estop_source": self.estop_source,
            "estop_at": iso(self.estop_at),
            "estop_input": {
                "configured": self.estop_input is not None,
                "pressed": self.estop_input_pressed,
                "error": self.estop_input_error,
                "hardware_cutoff": False,
            },
            "motion_permitted": permitted,
            "motion_blocked_reason": why,
            "obstacles": self.assessment.to_dict() if self.assessment else None,
        }


def checks_to_dicts(checks: list[PreflightCheck]) -> list[dict[str, Any]]:
    return [asdict(c) for c in checks]
