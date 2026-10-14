"""Sensor lifecycle management.

One :class:`SensorChannel` per configured sensor. Each enabled channel runs its own
asyncio task that initialises the driver, samples it at the configured rate in a
worker thread (with a timeout), tracks staleness and consecutive errors, and
retries initialisation after a disconnect. A failing optional sensor only raises
a WARNING fault and an unavailable status; it never stops the application.
Sensors listed in ``safety.required_for_arming`` raise an ERROR fault instead,
which blocks arming but not unrelated functionality.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from rover.config import RoverConfig
from rover.core.clock import iso
from rover.core.enums import Severity
from rover.core.events import EventLog, FaultManager
from rover.sensors.base import ConnectionState, ReadingHealth, Sensor, SensorReading
from rover.sensors.drivers import DriverContext, create_sensor, registered_drivers

log = logging.getLogger(__name__)

ReadingListener = Callable[[SensorReading], None]


class SensorChannel:
    def __init__(self, cfg: Any, required: bool) -> None:
        self.cfg = cfg
        self.required = required
        self.sensor: Sensor | None = None
        self.state = ConnectionState.CONFIGURED if cfg.enabled else ConnectionState.DISABLED
        self.error: str | None = None
        self.last_reading: SensorReading | None = None
        self.last_valid: SensorReading | None = None
        self.last_new_reading_mono: float | None = None
        self.last_attempt_mono: float | None = None
        self.consecutive_errors = 0
        self.ever_connected = False
        self.connected_since: float | None = None
        self.reconnects = 0
        self.readings_total = 0
        self.invalid_total = 0
        cls = registered_drivers().get((cfg.type, cfg.driver))
        # Simulated status comes from the driver class, never from a guess.
        self.simulated = bool(cls.simulated) if cls else False
        self.physically_verified = bool(cls.physically_verified) if cls else False

    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def type(self) -> str:
        return self.cfg.type

    def reading_health(self, now: float | None = None) -> ReadingHealth:
        now = time.monotonic() if now is None else now
        r = self.last_reading
        if r is None or self.last_new_reading_mono is None:
            return ReadingHealth.NONE
        if now - self.last_new_reading_mono > self.cfg.effective_stale_s:
            return ReadingHealth.STALE
        return ReadingHealth.OK if r.valid else ReadingHealth.INVALID

    def usable_reading(self, now: float | None = None) -> SensorReading | None:
        """Latest reading if the device is connected, fresh and the reading is valid."""
        if self.state != ConnectionState.CONNECTED:
            return None
        if self.reading_health(now) != ReadingHealth.OK:
            return None
        return self.last_reading

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        r = self.last_reading
        age = None if self.last_new_reading_mono is None else round(now - self.last_new_reading_mono, 3)
        calibration: dict[str, Any] = {"state": "unknown"}
        if self.sensor is not None:
            try:
                calibration = self.sensor.calibration()
            except Exception as exc:  # pragma: no cover
                calibration = {"state": "error", "error": str(exc)}
        return {
            "id": self.id,
            "type": self.type,
            "label": self.cfg.label or self.id,
            "driver": self.cfg.driver,
            "enabled": self.cfg.enabled,
            "simulated": self.simulated,
            "physically_verified": self.physically_verified,
            "required_for_arming": self.required,
            "state": self.state.value,
            "reading_health": self.reading_health(now).value,
            "available": self.state == ConnectionState.CONNECTED,
            "error": self.error,
            "rate_hz": self.cfg.rate_hz,
            "stale_after_s": self.cfg.effective_stale_s,
            "age_s": age,
            "reconnects": self.reconnects,
            "readings_total": self.readings_total,
            "invalid_total": self.invalid_total,
            "last_reading": r.to_dict() if r else None,
            "last_valid_at": iso(self.last_valid.timestamp) if self.last_valid else None,
            "calibration": calibration,
            "safety_role": getattr(self.cfg, "safety_role", None),
        }


class SensorManager:
    def __init__(self, config: RoverConfig, ctx: DriverContext, events: EventLog, faults: FaultManager) -> None:
        self.config = config
        self.ctx = ctx
        self.events = events
        self.faults = faults
        required = set(config.safety.required_for_arming)
        self.channels: dict[str, SensorChannel] = {c.id: SensorChannel(c, c.id in required) for c in config.sensors}
        self._tasks: list[asyncio.Task[None]] = []
        self._listeners: list[ReadingListener] = []
        self._stopping = False

    # ------------------------------------------------------------------ queries

    def get(self, sensor_id: str) -> SensorChannel | None:
        return self.channels.get(sensor_id)

    def of_type(self, sensor_type: str, *, enabled_only: bool = True) -> list[SensorChannel]:
        return [c for c in self.channels.values() if c.type == sensor_type and (c.cfg.enabled or not enabled_only)]

    def add_listener(self, fn: ReadingListener) -> None:
        self._listeners.append(fn)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [c.snapshot(now) for c in self.channels.values()]

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        for ch in self.channels.values():
            if ch.cfg.enabled:
                self._tasks.append(asyncio.create_task(self._run(ch), name=f"sensor:{ch.id}"))
            else:
                log.info("sensor %s disabled in configuration", ch.id)

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        for ch in self.channels.values():
            self._close(ch)

    def _close(self, ch: SensorChannel) -> None:
        if ch.sensor is not None:
            try:
                ch.sensor.close()
            except Exception:  # pragma: no cover
                log.exception("closing sensor %s", ch.id)

    def _fault_code(self, ch: SensorChannel) -> str:
        return f"sensor_{ch.id}_unavailable"

    def _mark_down(self, ch: SensorChannel, state: ConnectionState, error: str) -> None:
        was = ch.state
        ch.state = state
        ch.error = error
        ch.connected_since = None
        if was != state:
            self.faults.raise_fault(
                self._fault_code(ch),
                Severity.ERROR if ch.required else Severity.WARNING,
                f"sensor.{ch.id}",
                f"{ch.cfg.label or ch.id} {state.value}: {error}",
            )

    async def _initialize(self, ch: SensorChannel) -> bool:
        ch.state = ConnectionState.INITIALIZING
        ch.last_attempt_mono = time.monotonic()
        try:
            if ch.sensor is None:
                ch.sensor = create_sensor(ch.cfg, self.ctx)
                ch.simulated = ch.sensor.simulated
                ch.physically_verified = ch.sensor.physically_verified
            await asyncio.wait_for(asyncio.to_thread(ch.sensor.initialize), timeout=max(5.0, ch.cfg.read_timeout_s))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # init failure is never fatal for the application
            self._close(ch)
            down = ConnectionState.DISCONNECTED if ch.ever_connected else ConnectionState.FAULTY
            self._mark_down(ch, down, f"{type(exc).__name__}: {exc}")
            return False
        if ch.ever_connected:
            ch.reconnects += 1
            self.events.emit(Severity.INFO, f"sensor.{ch.id}", "sensor.reconnected", f"{ch.cfg.label or ch.id} reconnected")
        else:
            self.events.emit(Severity.INFO, f"sensor.{ch.id}", "sensor.connected", f"{ch.cfg.label or ch.id} connected ({ch.cfg.driver})")
        ch.ever_connected = True
        ch.state = ConnectionState.CONNECTED
        ch.error = None
        ch.consecutive_errors = 0
        ch.connected_since = time.monotonic()
        self.faults.clear(self._fault_code(ch), by="recovered")
        return True

    async def _run(self, ch: SensorChannel) -> None:
        clock = getattr(self.ctx.world, "clock", None) if ch.simulated else None
        while not self._stopping:
            # Simulated sensors sample at their rate in simulation time.
            scale = max(getattr(clock, "scale", 1.0), 1.0) if clock is not None else 1.0
            period = 1.0 / (ch.cfg.rate_hz * scale)
            if ch.state != ConnectionState.CONNECTED:
                if not await self._initialize(ch):
                    await asyncio.sleep(ch.cfg.reconnect_interval_s)
                    continue
            t0 = time.monotonic()
            await self._sample(ch)
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

    async def _sample(self, ch: SensorChannel) -> None:
        assert ch.sensor is not None
        try:
            reading = await asyncio.wait_for(asyncio.to_thread(ch.sensor.acquire), timeout=ch.cfg.read_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ch.consecutive_errors += 1
            ch.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            if ch.consecutive_errors >= ch.cfg.max_consecutive_errors:
                self._close(ch)
                self._mark_down(ch, ConnectionState.DISCONNECTED, ch.error)
            return
        ch.consecutive_errors = 0
        if reading is None:
            return  # no new measurement; staleness is derived from the timestamp
        ch.last_reading = reading
        ch.last_new_reading_mono = reading.monotonic
        ch.readings_total += 1
        if reading.valid:
            ch.last_valid = reading
            ch.error = None
        else:
            ch.invalid_total += 1
            ch.error = reading.error
        for fn in self._listeners:
            try:
                fn(reading)
            except Exception:  # pragma: no cover
                log.exception("sensor listener failed")
