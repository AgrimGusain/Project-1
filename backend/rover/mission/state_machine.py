"""Explicit mission state machine. Every transition not listed here is rejected."""

from __future__ import annotations

from collections.abc import Callable

from rover.core.enums import MissionState as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.IDLE: frozenset({S.READY, S.EMERGENCY_STOP}),
    S.READY: frozenset({S.IDLE, S.ARMING, S.EMERGENCY_STOP}),
    S.ARMING: frozenset({S.RUNNING, S.READY, S.FAILED, S.STOPPING, S.EMERGENCY_STOP}),
    S.RUNNING: frozenset({S.PAUSED, S.STOPPING, S.COMPLETED, S.FAILED, S.EMERGENCY_STOP}),
    S.PAUSED: frozenset({S.RUNNING, S.STOPPING, S.FAILED, S.EMERGENCY_STOP}),
    S.STOPPING: frozenset({S.IDLE, S.FAILED, S.EMERGENCY_STOP}),
    S.COMPLETED: frozenset({S.IDLE, S.EMERGENCY_STOP}),
    S.FAILED: frozenset({S.IDLE, S.EMERGENCY_STOP}),
    S.EMERGENCY_STOP: frozenset({S.IDLE}),
}

# States in which the mission executor may own the motion controller.
ACTIVE_STATES = frozenset({S.ARMING, S.RUNNING, S.PAUSED, S.STOPPING})


class InvalidTransition(Exception):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"invalid mission transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


class MissionStateMachine:
    def __init__(self) -> None:
        self.state = S.IDLE
        self.listeners: list[Callable[[S, S, str], None]] = []

    def can(self, target: S) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: S, reason: str = "") -> S:
        if not self.can(target):
            raise InvalidTransition(self.state, target)
        prev, self.state = self.state, target
        for cb in self.listeners:
            cb(prev, target, reason)
        return prev
