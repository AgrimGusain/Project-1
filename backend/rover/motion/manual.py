"""Manual remote control with a server-side dead-man.

The browser sends a drive message every ``heartbeat_interval_s`` while a control
is held. Each message renews a motion lease that expires after
``deadman_timeout_s``. Releasing sends an explicit stop. If messages stop for any
reason (tab closed, Wi-Fi drop, frozen browser), the lease lapses and the
controller outputs zero — the frontend is never trusted to enforce safety.

* One manual session at a time; a second client must explicitly take over.
* Sequence numbers must increase; stale/replayed messages are rejected.
* A reconnect creates a new session and never resumes previous motion.
* Continuous motion is limited to ``max_continuous_s``; then the operator must
  release and press again.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from rover.config import RoverConfig
from rover.core.enums import ControlOwner, Severity
from rover.core.events import EventLog
from rover.motion.controller import CommandRejected, MotionController

Direction = Literal[
    "stop", "forward", "reverse", "left", "right",
    "forward_left", "forward_right", "reverse_left", "reverse_right",
]


def direction_to_tank(direction: str, speed: float, turn_ratio: float) -> tuple[float, float]:
    """Map a direction button to (left, right) outputs for tank/skid steering."""
    r = turn_ratio
    table: dict[str, tuple[float, float]] = {
        "stop": (0.0, 0.0),
        "forward": (1.0, 1.0),
        "reverse": (-1.0, -1.0),
        "left": (-1.0, 1.0),  # pivot counter-clockwise
        "right": (1.0, -1.0),  # pivot clockwise
        "forward_left": (r, 1.0),
        "forward_right": (1.0, r),
        "reverse_left": (-r, -1.0),  # tail swings left
        "reverse_right": (-1.0, -r),
    }
    if direction not in table:
        raise CommandRejected("invalid_command", f"unknown direction '{direction}'")
    left, right = table[direction]
    return left * speed, right * speed


def vector_to_tank(throttle: float, steer: float) -> tuple[float, float]:
    """Arcade-to-tank mixing for a joystick. throttle/steer in [-1, 1]."""
    left = throttle + steer
    right = throttle - steer
    peak = max(1.0, abs(left), abs(right))
    return left / peak, right / peak


def _finite(v: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not (lo <= v <= hi):
        raise CommandRejected("invalid_command", f"{name} must be a number in [{lo}, {hi}]")
    return float(v)


@dataclass
class ManualSession:
    session_id: str
    client_id: str
    opened_at: float
    last_message_at: float
    last_seq: int = -1
    motion_started_at: float | None = None
    hold_limit_hit: bool = False
    last_command: tuple[float, float] = (0.0, 0.0)
    deadman_trips: int = 0


class ManualControlService:
    def __init__(self, config: RoverConfig, controller: MotionController, events: EventLog) -> None:
        self.cfg = config.manual
        self.controller = controller
        self.events = events
        self.session: ManualSession | None = None
        self.link_dropped: Any = lambda: False  # simulation fault hook
        controller.on_lease_expired.append(self._lease_expired)

    # ------------------------------------------------------------------ sessions

    def open_session(self, client_id: str, takeover: bool = False) -> ManualSession:
        if self.controller.owner == ControlOwner.MISSION:
            raise CommandRejected("mission_active", "an autonomous mission owns the controller; stop it first")
        if self.session is not None:
            if not takeover:
                raise CommandRejected("session_busy", f"manual control is held by client {self.session.client_id}")
            self.close_session(self.session.session_id, f"taken over by {client_id}")
        sid = secrets.token_hex(8)
        self.controller.acquire(ControlOwner.MANUAL, sid)
        now = time.monotonic()
        self.session = ManualSession(sid, client_id, now, now)
        self.events.emit(Severity.INFO, "manual", "manual.session_opened", f"Manual control acquired by {client_id}")
        return self.session

    def close_session(self, session_id: str, reason: str = "released") -> None:
        s = self.session
        if s is None or s.session_id != session_id:
            return
        self.controller.zero(ControlOwner.MANUAL, session_id)
        self.controller.release(ControlOwner.MANUAL, session_id)
        self.session = None
        self.events.emit(Severity.INFO, "manual", "manual.session_closed", f"Manual control released ({reason})")

    def _require(self, session_id: str) -> ManualSession:
        s = self.session
        if s is None or s.session_id != session_id:
            raise CommandRejected("stale_session", "this control session is no longer active; re-acquire control")
        if not self.controller.is_owner(ControlOwner.MANUAL, session_id):
            raise CommandRejected("not_owner", "manual session does not own the controller")
        return s

    # ------------------------------------------------------------------ commands

    def drive(self, session_id: str, seq: Any, message: dict[str, Any]) -> dict[str, Any]:
        if self.link_dropped():
            return {"accepted": False, "code": "link_dropped", "message": "simulated link drop"}
        s = self._require(session_id)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= s.last_seq:
            raise CommandRejected("stale_command", f"sequence {seq!r} is not newer than {s.last_seq}")
        s.last_seq = seq
        s.last_message_at = time.monotonic()

        mode = message.get("mode", "direction")
        if mode == "direction":
            speed = _finite(message.get("speed", self.cfg.default_speed), "speed", 0.0, 1.0)
            speed = min(speed, self.cfg.max_speed)
            left, right = direction_to_tank(str(message.get("direction", "")), speed, self.cfg.turn_ratio)
        elif mode == "vector":
            throttle = _finite(message.get("throttle"), "throttle", -1.0, 1.0)
            steer = _finite(message.get("steer"), "steer", -1.0, 1.0)
            speed = min(_finite(message.get("speed", 1.0), "speed", 0.0, 1.0), self.cfg.max_speed)
            left, right = vector_to_tank(throttle, steer)
            left, right = left * speed, right * speed
        else:
            raise CommandRejected("invalid_command", f"unknown mode '{mode}'")

        now = time.monotonic()
        moving = abs(left) > 1e-3 or abs(right) > 1e-3
        if not moving:
            s.motion_started_at = None
            s.hold_limit_hit = False
            self.controller.command(ControlOwner.MANUAL, session_id, 0.0, 0.0, self.cfg.deadman_timeout_s)
            s.last_command = (0.0, 0.0)
            return {"accepted": True, "left": 0.0, "right": 0.0}
        if s.hold_limit_hit:
            raise CommandRejected("hold_limit", "continuous-drive limit reached: release the control and press again")
        if s.motion_started_at is None:
            s.motion_started_at = now
        elif now - s.motion_started_at > self.cfg.max_continuous_s:
            s.hold_limit_hit = True
            self.controller.zero(ControlOwner.MANUAL, session_id)
            self.events.emit(
                Severity.WARNING, "manual", "manual.hold_limit",
                f"Continuous manual drive exceeded {self.cfg.max_continuous_s:.0f} s; motors stopped",
            )
            raise CommandRejected("hold_limit", "continuous-drive limit reached: release the control and press again")
        self.controller.command(ControlOwner.MANUAL, session_id, left, right, self.cfg.deadman_timeout_s)
        s.last_command = (left, right)
        return {"accepted": True, "left": round(left, 3), "right": round(right, 3)}

    def stop(self, session_id: str | None = None) -> None:
        s = self.session
        if s is None or (session_id is not None and s.session_id != session_id):
            return
        s.motion_started_at = None
        s.hold_limit_hit = False
        s.last_command = (0.0, 0.0)
        self.controller.zero(ControlOwner.MANUAL, s.session_id)

    def _lease_expired(self, owner: ControlOwner, holder_id: str) -> None:
        s = self.session
        if owner == ControlOwner.MANUAL and s is not None and s.session_id == holder_id:
            s.deadman_trips += 1
            s.motion_started_at = None
            s.last_command = (0.0, 0.0)

    def snapshot(self) -> dict[str, Any]:
        s = self.session
        now = time.monotonic()
        return {
            "session_active": s is not None,
            "client_id": s.client_id if s else None,
            "last_message_age_s": round(now - s.last_message_at, 3) if s else None,
            "deadman_timeout_s": self.cfg.deadman_timeout_s,
            "heartbeat_interval_s": self.cfg.heartbeat_interval_s,
            "max_continuous_s": self.cfg.max_continuous_s,
            "continuous_s": round(now - s.motion_started_at, 2) if s and s.motion_started_at else 0.0,
            "hold_limit_hit": bool(s and s.hold_limit_hit),
            "deadman_trips": s.deadman_trips if s else 0,
            "last_command": {"left": s.last_command[0], "right": s.last_command[1]} if s else None,
            "default_speed": self.cfg.default_speed,
            "max_speed": self.cfg.max_speed,
        }
