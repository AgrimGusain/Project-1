"""Shared enumerations. String-valued so they serialise directly into JSON."""

from __future__ import annotations

from enum import StrEnum


class OperatingMode(StrEnum):
    SIMULATION = "simulation"
    HARDWARE = "hardware"
    HARDWARE_GPS = "hardware_gps"


class MissionState(StrEnum):
    IDLE = "IDLE"
    READY = "READY"
    ARMING = "ARMING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class MissionKind(StrEnum):
    GRID = "grid"
    GPS = "gps"


class MissionStatus(StrEnum):
    """Persistent status of a mission *record* (distinct from the live state machine)."""

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EMERGENCY_STOPPED = "emergency_stopped"
    INTERRUPTED = "interrupted"


class ControlOwner(StrEnum):
    NONE = "none"
    MANUAL = "manual"
    MISSION = "mission"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "error": 2, "critical": 3}[self.value]


class SensorHealth(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    STALE = "stale"
    INVALID = "invalid"
    STARTING = "starting"


class ObstacleState(StrEnum):
    CLEAR = "clear"
    OBSTACLE = "obstacle"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"
    DISABLED = "disabled"


class LocalizationStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    NO_FIX = "no_fix"
    DISCONNECTED = "disconnected"
    UNAVAILABLE = "unavailable"
    STARTING = "starting"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    CONFIRMED_SIGNAL = "confirmed_signal"
    FALSE_POSITIVE = "false_positive"
    DISMISSED = "dismissed"
    NEEDS_FOLLOW_UP = "needs_follow_up"
