"""Composition root: builds and wires every subsystem, owns background tasks.

Startup policy:
* the rover starts DISARMED with zero motor output;
* a real device that fails to initialise is reported as unavailable — it is
  never replaced by a simulated one;
* missions left active by a crash/reboot are marked INTERRUPTED and never resumed;
* the application keeps running when optional hardware is missing.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import time
from typing import Any

from rover import __version__
from rover.camera import CameraProvider, NoCamera, build_camera
from rover.capabilities import evaluate_all, to_dicts
from rover.config import RoverConfig
from rover.core.clock import Clock, ScaledClock, iso, utcnow
from rover.core.enums import ControlOwner, MissionState, Severity
from rover.core.events import EventLog, FaultManager
from rover.detection import DetectionRecorder, ThresholdProcessor
from rover.hal.estop import GpioEstopInput
from rover.hal.gpio import LazyGpio
from rover.hal.motors import L298NMotorDriver, MotorDriver, SimMotorDriver, UnavailableMotorDriver, WatchdogMotorDriver
from rover.localization.base import NoLocalizationProvider
from rover.localization.service import LocalizationService, build_provider
from rover.mission.manager import MissionManager
from rover.motion.controller import MotionController
from rover.motion.manual import ManualControlService
from rover.persistence.db import Database
from rover.persistence.repository import Repository
from rover.safety.supervisor import SafetySupervisor
from rover.sensors.drivers import DriverContext
from rover.sensors.manager import SensorManager
from rover.sim.world import SIM_FAULTS, SimWorld
from rover.vision.service import VisionService

log = logging.getLogger(__name__)


class RoverRuntime:
    def __init__(self, config: RoverConfig, db_path: str | None = None) -> None:
        self.config = config
        self.started_at = utcnow()
        self.events = EventLog()
        self.faults = FaultManager(self.events)
        self.db = Database.for_path(db_path or config.database.path)
        self.repo = Repository(self.db)
        sim = config.is_simulation
        self.clock: Clock = ScaledClock(config.simulation.speed_factor) if sim else Clock()
        self.world: SimWorld | None = SimWorld(config, self.clock) if sim else None
        self.gpio: LazyGpio | None = None if sim else LazyGpio(config.gpio.chip)
        self.hardware_errors: dict[str, str] = {}

        # Motors (never silently simulated in hardware mode).
        self.driver = self._build_driver()
        self.controller = MotionController(self.driver, config, self.clock, self.events, self.faults)
        if self.world is not None:
            world = self.world
            self.controller.stalled = lambda: world.has_fault("control_loop_stall")

        # Sensors
        self.sensors = SensorManager(config, DriverContext(config, self.gpio, self.world), self.events, self.faults)

        # Physical e-stop input (optional)
        self.estop_input: GpioEstopInput | None = None
        if config.safety.estop_input.enabled and self.gpio is not None:
            self.estop_input = GpioEstopInput(config.safety.estop_input, self.gpio)

        self.supervisor = SafetySupervisor(config, self.controller, self.sensors, self.faults, self.events, self.estop_input)
        self.manual = ManualControlService(config, self.controller, self.events)
        if self.world is not None:
            world = self.world
            self.manual.link_dropped = lambda: world.has_fault("control_link_drop")

        try:
            provider = build_provider(config, self.sensors, self.world)
        except Exception as exc:
            self.hardware_errors["localization"] = str(exc)
            provider = NoLocalizationProvider()
        self.localization = LocalizationService(config, provider, self.sensors)
        self.localization.motors_active = lambda: self.controller.applied != [0.0, 0.0]

        try:
            self.camera: CameraProvider = build_camera(config.camera, self.world)
        except Exception as exc:
            self.hardware_errors["camera"] = str(exc)
            self.camera = NoCamera()

        self.missions = MissionManager(
            config, self.repo, self.controller, self.supervisor, self.localization, self.events, self.faults, self.clock, self.world
        )
        self.events.mission_id_provider = self.missions.current_mission_id
        self.detections = DetectionRecorder(self.repo, self.events, self.localization, self.missions.current_mission_id)
        for ch in self.sensors.of_type("metal_detector"):
            c = ch.cfg
            self.detections.add_processor(ch.id, ThresholdProcessor(c.threshold, c.release_threshold, c.saturation, c.min_event_interval_s))
        self.sensors.add_listener(self.detections.on_reading)
        self.vision = VisionService(config.vision, self.camera, self.detections, self.events)

        self._tasks: list[asyncio.Task[Any]] = []
        self._telemetry_seq = 0
        self._stopped = False
        atexit.register(self._emergency_zero)

    # ------------------------------------------------------------------ construction

    def _build_driver(self) -> MotorDriver:
        cfg = self.config.motors

        def tripped() -> None:
            self.events.emit(Severity.ERROR, "motion", "motion.watchdog",
                             "Motor driver watchdog tripped: no output update received, motors forced to zero")

        if cfg.driver == "simulated":
            assert self.world is not None
            inner: MotorDriver = SimMotorDriver(self.world)
        else:
            assert self.gpio is not None
            inner = L298NMotorDriver(cfg, self.gpio)
        return WatchdogMotorDriver(inner, cfg.watchdog_timeout_s, on_trip=tripped)

    def _init_driver(self) -> None:
        try:
            self.driver.initialize()
        except Exception as exc:
            msg = f"motor driver failed to initialise: {exc}"
            self.hardware_errors["motors"] = msg
            self.faults.raise_fault("motor_driver_init", Severity.CRITICAL, "motion", msg)
            # Replace with a driver that refuses all motion; NOT a simulated one.
            self.driver = UnavailableMotorDriver(self.config.motors.driver, msg)
            self.controller.driver = self.driver

    def _emergency_zero(self) -> None:
        try:
            self.driver.stop()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)
        version = self.db.migrate()
        self.events.persist = self.repo.persist_event
        self.faults.persist_raise = lambda f: self.repo.persist_fault_raised(f, self.missions.current_mission_id())
        self.faults.persist_clear = self.repo.persist_fault_cleared
        self.repo.set_meta("last_start", {"at": iso(utcnow()), "mode": self.config.mode.value, "version": __version__,
                                          "config_fingerprint": self.config.fingerprint(), "schema_version": version})
        for m in self.repo.recover_interrupted():
            self.events.emit(Severity.WARNING, "mission", "mission.interrupted",
                             f"Mission '{m['name']}' was active when the backend stopped. It will NOT resume; review required.",
                             mission_id=m["id"])
        mode_label = {"simulation": "SIMULATION", "hardware": "REAL HARDWARE (no GPS)", "hardware_gps": "REAL HARDWARE + GPS"}
        self.events.emit(Severity.WARNING if not self.config.is_simulation else Severity.INFO, "system", "system.start",
                         f"Rover backend {__version__} started in {mode_label[self.config.mode.value]} mode; rover is DISARMED")

        self._init_driver()
        if self.estop_input is not None:
            try:
                self.estop_input.initialize()
            except Exception as exc:
                self.hardware_errors["estop_input"] = str(exc)
                self.faults.raise_fault("estop_input", Severity.CRITICAL, "safety", f"physical e-stop input unavailable: {exc}")
        for name, err in self.hardware_errors.items():
            if name not in ("motors", "estop_input"):
                self.faults.raise_fault(f"{name}_init", Severity.WARNING, name, err)

        self._spawn(self.controller.run(), "motion")
        if self.world is not None:
            self._spawn(self.world.run(), "sim-world")
        await self.sensors.start()
        self._spawn(self.supervisor.run(), "safety")
        try:
            await self.localization.start()
        except Exception as exc:
            self.faults.raise_fault("localization_start", Severity.WARNING, "localization", str(exc))
        await self.camera.start()
        await self.vision.start()
        self._spawn(self.missions.monitor(), "mission-monitor")
        self._spawn(self._persist_loop(), "persist")
        self._spawn(self._maintenance_loop(), "maintenance")

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self._stopped:
            return
        exc = task.exception()
        if exc is not None:
            log.error("background task %s crashed: %s", task.get_name(), exc, exc_info=exc)
            self.faults.raise_fault(f"task_{task.get_name()}", Severity.CRITICAL, "system", f"background task {task.get_name()} crashed: {exc}")
            if task.get_name() in ("motion", "safety"):
                self.supervisor.emergency_stop("system", f"{task.get_name()} task crashed")

    async def stop(self) -> None:
        if self._stopped:
            return
        # Motors first.
        self.controller.emergency_halt("backend shutdown")
        if self.missions.state in (MissionState.RUNNING, MissionState.PAUSED, MissionState.ARMING):
            try:
                await self.missions.stop(by="system", reason="backend shutdown")
            except Exception:  # pragma: no cover
                log.exception("mission stop on shutdown failed")
        self._stopped = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.vision.stop()
        await self.camera.stop()
        await self.localization.stop()
        await self.sensors.stop()
        self.driver.close()
        if self.estop_input is not None:
            self.estop_input.close()
        if self.gpio is not None:
            self.gpio.close()
        self.db.close()
        atexit.unregister(self._emergency_zero)

    # ------------------------------------------------------------------ background loops

    async def _persist_loop(self) -> None:
        period = 1.0 / self.config.database.telemetry_sample_hz
        while True:
            await asyncio.sleep(period)
            mid = self.missions.current_mission_id()
            active = self.missions.state in (MissionState.RUNNING, MissionState.PAUSED)
            if not active and not self.config.database.record_idle_telemetry:
                continue
            try:
                await asyncio.to_thread(self._persist_once, mid if active else None)
            except Exception:  # pragma: no cover
                log.exception("telemetry persistence failed")

    def _persist_once(self, mission_id: str | None) -> None:
        snap = self.missions.snapshot()
        progress = (snap.get("mission") or {}).get("progress") or {}
        est = self.localization.estimate()
        self.repo.add_telemetry(mission_id, {
            "state": snap["state"],
            "applied": self.controller.snapshot()["applied"],
            "progress_percent": progress.get("percent"),
            "estimated_pose": progress.get("estimated_pose"),
            "measured_pose": progress.get("measured_pose"),
            "localization": {"valid": est.position_valid, "status": est.status.value, "lat": est.lat, "lon": est.lon,
                             "accuracy_m": est.horizontal_accuracy_m},
            "truth": self.world.truth()["pose"] if self.world else None,
        })
        samples = []
        for ch in self.sensors.channels.values():
            r = ch.last_reading
            if ch.cfg.enabled and r is not None:
                samples.append({"sensor_id": ch.id, "sensor_type": ch.type, "valid": r.valid and ch.reading_health().value == "ok",
                                "simulated": r.simulated, "value": r.value, "error": r.error})
        self.repo.add_sensor_samples(mission_id, samples)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                n = await asyncio.to_thread(self.repo.prune, self.config.database.retention_days)
                if n:
                    log.info("pruned %s old telemetry rows", n)
            except Exception:  # pragma: no cover
                log.exception("prune failed")
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------ snapshots

    def capabilities(self) -> list[dict[str, Any]]:
        return to_dicts(evaluate_all(self))

    def telemetry(self) -> dict[str, Any]:
        self._telemetry_seq += 1
        cfg = self.config
        return {
            "type": "telemetry",
            "seq": self._telemetry_seq,
            "ts": iso(utcnow()),
            "mode": cfg.mode.value,
            "simulated": cfg.is_simulation,
            "rover_name": cfg.rover_name,
            "safety": self.supervisor.snapshot(),
            "motion": self.controller.snapshot(),
            "manual": self.manual.snapshot(),
            "mission": self.missions.snapshot(),
            "localization": self.localization.snapshot(),
            "sensors": self.sensors.snapshot(),
            "faults": [f.to_dict() for f in self.faults.active()],
            "camera": self.camera.status(),
            "vision": {**self.vision.status(), "latest": self.vision.latest_dict()},
            "capabilities": self.capabilities(),
            "simulation": self.sim_snapshot(),
        }

    def sim_snapshot(self) -> dict[str, Any] | None:
        if self.world is None:
            return None
        assert isinstance(self.clock, ScaledClock)
        return {
            "speed_factor": self.clock.scale,
            "truth": self.world.truth(),
            "faults": sorted(self.world.faults),
            "available_faults": SIM_FAULTS,
            "obstacles": [o.model_dump() for o in self.world.obstacles],
            "metal_targets": [t.model_dump() for t in self.config.simulation.metal_targets],
            "geo_origin": self.config.simulation.geo_origin.model_dump(),
        }

    def system_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "mode": self.config.mode.value,
            "simulated": self.config.is_simulation,
            "rover_name": self.config.rover_name,
            "started_at": iso(self.started_at),
            "uptime_s": round((utcnow() - self.started_at).total_seconds(), 1),
            "config_fingerprint": self.config.fingerprint(),
            "hardware_errors": self.hardware_errors,
            "motor_driver": self.controller.driver.status(),
            "gpio_chip": None if self.gpio is None or not self.gpio.opened else {
                "number": self.gpio._chip.chip_number, "label": self.gpio._chip.label},
            "auth_enabled": self.config.auth.enabled,
            "control_owner": self.controller.owner.value if self.controller.owner != ControlOwner.NONE else None,
            "ui": self.config.ui.model_dump(),
            "manual": {
                "deadman_timeout_s": self.config.manual.deadman_timeout_s,
                "heartbeat_interval_s": self.config.manual.heartbeat_interval_s,
                "default_speed": self.config.manual.default_speed,
                "max_speed": self.config.manual.max_speed,
            },
            "calibration": self.config.calibration.model_dump() | {"full_pivot_dps": round(self.config.calibration.full_pivot_dps, 1)},
            "time": time.time(),
        }
