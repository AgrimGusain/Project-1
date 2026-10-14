"""Operator-facing event log and fault registry.

``EventLog.emit`` is thread-safe: watchdog threads and sensor worker threads may
emit events. Subscribers (WebSocket clients) receive events on the asyncio loop.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rover.core.clock import iso, utcnow
from rover.core.enums import Severity

log = logging.getLogger("rover.events")


@dataclass
class LogEvent:
    id: int
    ts: datetime
    level: Severity
    source: str
    code: str
    message: str
    mission_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": iso(self.ts),
            "level": self.level.value,
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "mission_id": self.mission_id,
            "data": self.data,
        }


class EventLog:
    def __init__(self, maxlen: int = 500) -> None:
        self._events: deque[LogEvent] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[LogEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.persist: Callable[[LogEvent], None] | None = None
        self.mission_id_provider: Callable[[], str | None] | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(
        self,
        level: Severity | str,
        source: str,
        code: str,
        message: str,
        *,
        mission_id: str | None = None,
        **data: Any,
    ) -> LogEvent:
        level = Severity(level)
        if mission_id is None and self.mission_id_provider is not None:
            try:
                mission_id = self.mission_id_provider()
            except Exception:  # pragma: no cover
                mission_id = None
        with self._lock:
            ev = LogEvent(next(self._ids), utcnow(), level, source, code, message, mission_id, data)
            self._events.append(ev)
        py_level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL}
        log.log(py_level[level.value], "[%s/%s] %s", source, code, message)
        if self.persist is not None:
            try:
                self.persist(ev)
            except Exception:  # never let persistence failures break control paths
                log.exception("failed to persist event")
        self._fanout(ev)
        return ev

    def _fanout(self, ev: LogEvent) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _put() -> None:
            for q in list(self._subscribers):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover
                        pass
                q.put_nowait(ev)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _put()
        else:
            loop.call_soon_threadsafe(_put)

    def recent(self, limit: int = 100, min_level: Severity | None = None) -> list[LogEvent]:
        with self._lock:
            items = list(self._events)
        if min_level is not None:
            items = [e for e in items if e.level.rank >= min_level.rank]
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue[LogEvent]:
        q: asyncio.Queue[LogEvent] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[LogEvent]) -> None:
        self._subscribers.discard(q)


@dataclass
class Fault:
    code: str
    severity: Severity
    source: str
    message: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    active: bool = True

    @property
    def blocks_arming(self) -> bool:
        return self.severity.rank >= Severity.ERROR.rank

    @property
    def blocks_motion(self) -> bool:
        return self.severity == Severity.CRITICAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "source": self.source,
            "message": self.message,
            "first_seen": iso(self.first_seen),
            "last_seen": iso(self.last_seen),
            "count": self.count,
            "active": self.active,
            "blocks_arming": self.blocks_arming,
            "blocks_motion": self.blocks_motion,
        }


class FaultManager:
    """Active fault conditions. ERROR blocks arming; CRITICAL also blocks motion."""

    def __init__(self, events: EventLog) -> None:
        self.events = events
        self._faults: dict[str, Fault] = {}
        self._lock = threading.Lock()
        self.persist_raise: Callable[[Fault], None] | None = None
        self.persist_clear: Callable[[Fault, str], None] | None = None

    def raise_fault(self, code: str, severity: Severity | str, source: str, message: str) -> Fault:
        severity = Severity(severity)
        now = utcnow()
        with self._lock:
            existing = self._faults.get(code)
            if existing and existing.active:
                existing.last_seen = now
                existing.count += 1
                if existing.severity.rank < severity.rank or existing.message != message:
                    existing.severity = severity
                    existing.message = message
                return existing
            fault = Fault(code, severity, source, message, now, now)
            self._faults[code] = fault
        self.events.emit(severity, source, f"fault.{code}", message)
        if self.persist_raise:
            try:
                self.persist_raise(fault)
            except Exception:  # pragma: no cover
                log.exception("failed to persist fault")
        return fault

    def clear(self, code: str, by: str = "system") -> bool:
        with self._lock:
            fault = self._faults.pop(code, None)
        if fault is None:
            return False
        fault.active = False
        self.events.emit(Severity.INFO, fault.source, f"fault_cleared.{code}", f"Cleared ({by}): {fault.message}")
        if self.persist_clear:
            try:
                self.persist_clear(fault, by)
            except Exception:  # pragma: no cover
                log.exception("failed to persist fault clear")
        return True

    def is_active(self, code: str) -> bool:
        with self._lock:
            return code in self._faults

    def active(self) -> list[Fault]:
        with self._lock:
            return sorted(self._faults.values(), key=lambda f: (-f.severity.rank, f.first_seen))

    def blocking_arming(self) -> list[Fault]:
        return [f for f in self.active() if f.blocks_arming]

    def blocking_motion(self) -> list[Fault]:
        return [f for f in self.active() if f.blocks_motion]
