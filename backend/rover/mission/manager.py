"""Mission manager: the only component that runs missions.

HTTP/WebSocket handlers call into the manager; they never run their own motion
loops. The manager enforces the state machine, interlocks with manual control
(through motion-controller ownership), persists every transition and never
auto-resumes a mission after an e-stop or a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rover.config import RoverConfig
from rover.core.clock import Clock, utcnow
from rover.core.enums import ControlOwner, MissionState, MissionStatus, Severity
from rover.core.events import EventLog, FaultManager
from rover.localization.service import LocalizationService
from rover.mission.executor import ExecMode, ExecutorHooks, MissionAbort, MissionExecutor
from rover.mission.state_machine import ACTIVE_STATES, InvalidTransition, MissionStateMachine
from rover.motion.controller import CommandRejected, MotionController
from rover.persistence.repository import Repository
from rover.planning.coverage import PlanningError
from rover.planning.models import CoveragePlan, PlanIssue
from rover.planning.service import plan_from_request
from rover.safety.supervisor import SafetySupervisor

log = logging.getLogger(__name__)


class MissionError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class ActiveMission:
    record: dict[str, Any]
    plan: CoveragePlan
    execution: ExecMode
    executor: MissionExecutor | None = None
    task: asyncio.Task[None] | None = None
    holder_id: str = ""
    pause_reason: str | None = None
    paused_at_mono: float | None = None
    invalidated: str | None = None
    message: str = ""


class MissionManager:
    def __init__(
        self,
        config: RoverConfig,
        repo: Repository,
        controller: MotionController,
        supervisor: SafetySupervisor,
        localization: LocalizationService,
        events: EventLog,
        faults: FaultManager,
        clock: Clock,
        world: Any | None = None,
    ) -> None:
        self.cfg = config
        self.repo = repo
        self.controller = controller
        self.supervisor = supervisor
        self.localization = localization
        self.events = events
        self.faults = faults
        self.clock = clock
        self.world = world
        self.sm = MissionStateMachine()
        self.sm.listeners.append(self._on_transition)
        self.active: ActiveMission | None = None
        self.last_result: dict[str, Any] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self.capability_check: Callable[[str], Any] | None = None
        supervisor.on_emergency_stop.append(self._on_estop)
        supervisor.on_estop_reset.append(self._on_estop_reset)
        supervisor.on_disarm.append(self._on_disarm)

    # ------------------------------------------------------------------ helpers

    @property
    def state(self) -> MissionState:
        return self.sm.state

    def current_mission_id(self) -> str | None:
        return self.active.record["id"] if self.active else None

    def _on_transition(self, prev: MissionState, new: MissionState, reason: str) -> None:
        mid = self.current_mission_id()
        level = Severity.WARNING if new in (MissionState.FAILED, MissionState.EMERGENCY_STOP, MissionState.PAUSED) else Severity.INFO
        self.events.emit(level, "mission", "mission.state", f"Mission {prev.value} -> {new.value}" + (f": {reason}" if reason else ""),
                         mission_id=mid, from_state=prev.value, to_state=new.value)
        if mid:
            try:
                self.repo.add_mission_event(mid, "state_transition", reason, prev.value, new.value)
            except Exception:  # pragma: no cover
                log.exception("failed to persist mission transition")

    def _transition(self, target: MissionState, reason: str = "") -> None:
        try:
            self.sm.transition(target, reason)
        except InvalidTransition as exc:
            raise MissionError("invalid_transition", str(exc)) from exc

    def _require(self, *states: MissionState) -> None:
        if self.sm.state not in states:
            raise MissionError("invalid_state", f"operation not allowed in state {self.sm.state.value}")

    def _execution_mode(self, plan: CoveragePlan) -> ExecMode:
        provider = self.localization.provider
        if plan.kind == "gps":
            return "gnss"
        if provider.kind == "local":
            return "closed_loop"
        return "open_loop"

    def _save_progress(self) -> None:
        a = self.active
        if a and a.executor:
            self.repo.update_mission(a.record["id"], progress=a.executor.progress.to_dict(a.execution, len(a.plan.segments)))

    # ------------------------------------------------------------------ mission records

    def create(self, name: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = plan_from_request(self.cfg, kind, params)
        except PlanningError:
            raise
        if not plan.valid:
            raise PlanningError([i for i in plan.issues if i.severity == "error"] or [
                PlanIssue(severity="error", code="invalid_plan", message="plan is not valid")])
        return self.repo.create_mission(
            id=str(uuid.uuid4()), name=name.strip()[:120] or f"{kind} survey", kind=kind,
            operating_mode=self.cfg.mode.value, status=MissionStatus.PLANNED.value, params=params,
            plan=plan.model_dump(mode="json"), plan_hash=plan.plan_hash, summary=plan.summary.model_dump(mode="json"),
            config_fingerprint=self.cfg.fingerprint(),
        )

    def replan(self, mission_id: str, name: str | None, params: dict[str, Any]) -> dict[str, Any]:
        rec = self.repo.get_mission(mission_id, include_plan=False)
        if rec is None:
            raise MissionError("not_found", "mission not found")
        if rec["status"] != MissionStatus.PLANNED.value:
            raise MissionError("not_editable", "only planned missions that have not run can be re-planned")
        if self.active and self.active.record["id"] == mission_id:
            raise MissionError("loaded", "unload the mission before re-planning it")
        plan = plan_from_request(self.cfg, rec["kind"], params)
        if not plan.valid:
            raise PlanningError([i for i in plan.issues if i.severity == "error"])
        out = self.repo.update_mission(
            mission_id, params=params, plan=plan.model_dump(mode="json"), plan_hash=plan.plan_hash,
            summary=plan.summary.model_dump(mode="json"), **({"name": name} if name else {}),
        )
        self.repo.add_mission_event(mission_id, "replanned", f"plan {plan.plan_hash}")
        assert out is not None
        return out

    def delete(self, mission_id: str) -> None:
        if self.active and self.active.record["id"] == mission_id:
            raise MissionError("loaded", "cannot delete the loaded mission")
        rec = self.repo.get_mission(mission_id, include_plan=False)
        if rec is None:
            raise MissionError("not_found", "mission not found")
        if rec["status"] == MissionStatus.ACTIVE.value:
            raise MissionError("active", "cannot delete an active mission")
        self.repo.delete_mission(mission_id)

    def acknowledge_review(self, mission_id: str, note: str | None) -> dict[str, Any]:
        rec = self.repo.update_mission(mission_id, review_required=False, reviewed_at=utcnow(), review_note=note)
        if rec is None:
            raise MissionError("not_found", "mission not found")
        self.repo.add_mission_event(mission_id, "reviewed", note or "")
        return rec

    # ------------------------------------------------------------------ lifecycle

    def load(self, mission_id: str) -> dict[str, Any]:
        if self.sm.state in (MissionState.COMPLETED, MissionState.FAILED):
            self.acknowledge()
        self._require(MissionState.IDLE)
        rec = self.repo.get_mission(mission_id)
        if rec is None:
            raise MissionError("not_found", "mission not found")
        if rec["status"] != MissionStatus.PLANNED.value:
            raise MissionError("not_runnable", f"mission status is '{rec['status']}'; only planned missions can be loaded. "
                               "Duplicate it to run again.")
        if rec["operating_mode"] != self.cfg.mode.value:
            raise MissionError("mode_mismatch", f"mission was planned for '{rec['operating_mode']}' but the rover runs in '{self.cfg.mode.value}'")
        plan = CoveragePlan.model_validate(rec["plan"])
        if not plan.valid:
            raise MissionError("invalid_plan", "stored plan is invalid")
        self.active = ActiveMission(record=rec, plan=plan, execution=self._execution_mode(plan))
        self._transition(MissionState.READY, f"loaded '{rec['name']}'")
        return rec

    def unload(self) -> None:
        self._require(MissionState.READY)
        self._transition(MissionState.IDLE, "unloaded")
        self.active = None

    def acknowledge(self) -> None:
        self._require(MissionState.COMPLETED, MissionState.FAILED)
        self._transition(MissionState.IDLE, "acknowledged")
        self.active = None

    def _start_checks(self, a: ActiveMission) -> list[str]:
        problems = []
        if not self.supervisor.armed:
            problems.append("rover is disarmed: arm it first")
        ok, why = self.supervisor.motion_permitted()
        if not ok and self.supervisor.armed:
            problems.append(why or "motion not permitted")
        if self.controller.owner == ControlOwner.MANUAL:
            problems.append("manual control session is active: release manual control first")
        if a.plan.kind == "gps":
            est = self.localization.estimate()
            if self.localization.provider.kind != "geodetic":
                problems.append("GPS mission requires a GNSS receiver (none configured)")
            elif not est.position_valid:
                problems.append(f"GPS fix not valid: {est.message}")
            elif not self._sim_places_rover() and a.plan.origin is not None and est.lat is not None and est.lon is not None:
                from rover.planning.geo import EnuFrame

                x, y = EnuFrame(a.plan.origin.lat, a.plan.origin.lon).to_enu(est.lat, est.lon)
                d = ((x - a.plan.start_pose.x) ** 2 + (y - a.plan.start_pose.y) ** 2) ** 0.5
                limit = self.cfg.localization.max_approach_distance_m
                if d > limit:
                    problems.append(f"rover is {d:.1f} m from the mission start (limit {limit:.0f} m); drive it closer manually")
        elif a.execution == "closed_loop" and self.localization.provider.name != "odometry":
            if not self.localization.estimate().position_valid:
                problems.append("local position source not valid")
        assessment = self.supervisor.fresh_assessment()
        if assessment.block_forward:
            problems.append("forward path blocked: " + "; ".join(assessment.front.reasons))
        return problems

    async def start(self, by: str = "operator") -> None:
        self._require(MissionState.READY)
        a = self.active
        assert a is not None
        problems = self._start_checks(a)
        self._transition(MissionState.ARMING, f"start requested by {by}")
        if problems:
            self._transition(MissionState.READY, "pre-start checks failed: " + "; ".join(problems))
            raise MissionError("start_refused", "pre-start checks failed", problems)
        a.holder_id = f"mission:{a.record['id']}"
        try:
            self.controller.acquire(ControlOwner.MISSION, a.holder_id)
        except CommandRejected as exc:
            self._transition(MissionState.READY, exc.message)
            raise MissionError("controller_busy", exc.message) from exc
        self._start_task = asyncio.create_task(self._countdown_and_run(a, by), name="mission-start")

    async def _countdown_and_run(self, a: ActiveMission, by: str) -> None:
        countdown = self.cfg.mission.start_countdown_s
        if countdown > 0:
            a.message = f"starting in {countdown:.0f} s"
            self.events.emit(Severity.WARNING, "mission", "mission.countdown",
                             f"Mission starting in {countdown:.0f} s — stand clear", mission_id=a.record["id"])
            end = time.monotonic() + countdown
            while time.monotonic() < end:
                await asyncio.sleep(0.05)
                if self.sm.state != MissionState.ARMING or self.active is not a:
                    return
                ok, why = self.supervisor.motion_permitted()
                if not ok:
                    self.controller.release(ControlOwner.MISSION, a.holder_id)
                    self._transition(MissionState.READY, f"start aborted: {why}")
                    return
        if self.sm.state != MissionState.ARMING or self.active is not a:
            return
        self._prepare_start_pose(a)
        hooks = ExecutorHooks(
            request_pause=self._executor_pause,
            obstacle_blocked=self._obstacle_reason,
            localization=self.localization.estimate,
        )
        a.executor = MissionExecutor(a.plan, a.execution, self.cfg, self.controller, self.clock, a.holder_id, hooks)
        now = utcnow()
        self.repo.update_mission(a.record["id"], status=MissionStatus.ACTIVE.value, started_at=now, execution=a.execution)
        self._transition(MissionState.RUNNING, f"started by {by} ({a.execution})")
        a.message = ""
        a.task = asyncio.create_task(self._run_executor(a), name="mission-executor")

    def _sim_places_rover(self) -> bool:
        return self.world is not None and self.cfg.simulation.place_rover_at_start

    def _prepare_start_pose(self, a: ActiveMission) -> None:
        sp = a.plan.start_pose
        if self._sim_places_rover():
            # Simulation stands in for the operator placing the rover at the start pose.
            if a.plan.kind == "gps" and a.plan.origin is not None:
                from rover.planning.geo import EnuFrame

                lat, lon = EnuFrame(a.plan.origin.lat, a.plan.origin.lon).to_geodetic(sp.x, sp.y)
                o = self.cfg.simulation.geo_origin
                x, y = EnuFrame(o.lat, o.lon, o.alt).to_enu(lat, lon)
            else:
                x, y = sp.x, sp.y
            assert self.world is not None
            self.world.reset(x, y, sp.heading_deg)
            self.events.emit(Severity.INFO, "simulation", "sim.placed",
                             f"Simulated rover placed at mission start ({x:.2f}, {y:.2f}, {sp.heading_deg:.0f}°)")
        self.localization.provider.reset_local_pose(sp.x, sp.y, sp.heading_deg)

    async def _run_executor(self, a: ActiveMission) -> None:
        assert a.executor is not None
        try:
            await a.executor.run()
        except asyncio.CancelledError:
            raise
        except MissionAbort as exc:
            self._finish(a, MissionState.FAILED, MissionStatus.FAILED, str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("mission executor crashed")
            self._finish(a, MissionState.FAILED, MissionStatus.FAILED, f"executor error: {exc}")
            return
        if self.active is a and self.sm.state == MissionState.RUNNING:
            self._finish(a, MissionState.COMPLETED, MissionStatus.COMPLETED, "all segments executed")

    def _finish(self, a: ActiveMission, state: MissionState, status: MissionStatus, reason: str) -> None:
        self.controller.zero(ControlOwner.MISSION, a.holder_id)
        self.controller.release(ControlOwner.MISSION, a.holder_id)
        if self.sm.can(state):
            self._transition(state, reason)
        fields: dict[str, Any] = {"status": status.value, "ended_reason": reason}
        if status == MissionStatus.COMPLETED:
            fields["completed_at"] = utcnow()
        elif status == MissionStatus.CANCELLED:
            fields["cancelled_at"] = utcnow()
        else:
            fields["completed_at"] = utcnow()
        if a.executor:
            fields["progress"] = a.executor.progress.to_dict(a.execution, len(a.plan.segments))
        self.repo.update_mission(a.record["id"], **fields)
        self.last_result = {"mission_id": a.record["id"], "status": status.value, "reason": reason}

    def _executor_pause(self, reason: str) -> None:
        if self.sm.state == MissionState.RUNNING:
            if reason.startswith("obstacle") and self.cfg.safety.obstacle_policy == "stop":
                a = self.active
                assert a is not None
                if a.task:
                    a.task.cancel()
                self._finish(a, MissionState.FAILED, MissionStatus.FAILED, f"stopped by obstacle policy: {reason}")
                return
            self.pause(reason, by="system")

    def _obstacle_reason(self) -> str | None:
        a = self.supervisor.assessment
        if a and a.block_forward:
            return "; ".join(a.front.reasons) or a.front.state.value
        return None

    def pause(self, reason: str = "operator request", by: str = "operator") -> None:
        self._require(MissionState.RUNNING)
        a = self.active
        assert a is not None and a.executor is not None
        a.executor.pause(reason)
        a.pause_reason = reason
        a.paused_at_mono = time.monotonic()
        self.controller.zero(ControlOwner.MISSION, a.holder_id)
        self._transition(MissionState.PAUSED, f"{reason} (by {by})")
        self.repo.update_mission(a.record["id"], paused_at=utcnow())
        self._save_progress()

    def resume(self, by: str = "operator") -> None:
        self._require(MissionState.PAUSED)
        a = self.active
        assert a is not None and a.executor is not None
        if a.invalidated:
            raise MissionError("invalidated", f"mission cannot be resumed: {a.invalidated}")
        problems = []
        ok, why = self.supervisor.motion_permitted()
        if not ok:
            problems.append(why or "motion not permitted")
        if a.execution in ("gnss", "closed_loop"):
            est = self.localization.estimate()
            if not est.position_valid:
                problems.append(f"localization not valid: {est.message}")
        self.supervisor.fresh_assessment()
        reason = self._obstacle_reason()
        seg = a.plan.segments[a.executor.progress.segment_index] if a.plan.segments else None
        if reason and seg is not None and seg.kind.value != "pivot":
            problems.append(f"obstacle still present: {reason}")
        if problems:
            raise MissionError("resume_refused", "cannot resume", problems)
        a.pause_reason = None
        a.paused_at_mono = None
        a.executor.resume()
        self._transition(MissionState.RUNNING, f"resumed by {by}")
        self.repo.update_mission(a.record["id"], resumed_at=utcnow())

    async def stop(self, by: str = "operator", reason: str = "cancelled by operator") -> None:
        self._require(MissionState.ARMING, MissionState.RUNNING, MissionState.PAUSED)
        a = self.active
        assert a is not None
        self.controller.zero(ControlOwner.MISSION, a.holder_id)
        self._transition(MissionState.STOPPING, f"{reason} (by {by})")
        for t in (a.task, self._start_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self.controller.zero(ControlOwner.MISSION, a.holder_id)
        self.controller.release(ControlOwner.MISSION, a.holder_id)
        started = a.executor is not None
        fields: dict[str, Any] = {"status": MissionStatus.CANCELLED.value if started else MissionStatus.PLANNED.value}
        if started:
            fields.update(cancelled_at=utcnow(), ended_reason=reason, progress=a.executor.progress.to_dict(a.execution, len(a.plan.segments)))  # type: ignore[union-attr]
        self.repo.update_mission(a.record["id"], **fields)
        self.last_result = {"mission_id": a.record["id"], "status": fields["status"], "reason": reason}
        self._transition(MissionState.IDLE, "stopped")
        self.active = None

    # ------------------------------------------------------------------ safety callbacks

    def _on_estop(self, reason: str) -> None:
        a = self.active
        state = self.sm.state
        if state == MissionState.EMERGENCY_STOP:
            return
        if a is not None:
            for t in (a.task, self._start_task):
                if t and not t.done():
                    t.cancel()
            self.controller.release(ControlOwner.MISSION, a.holder_id)
            a.invalidated = f"emergency stop: {reason}"
            if state in ACTIVE_STATES and a.executor is not None:
                self.repo.update_mission(
                    a.record["id"], status=MissionStatus.EMERGENCY_STOPPED.value, completed_at=utcnow(),
                    ended_reason=f"emergency stop: {reason}",
                    progress=a.executor.progress.to_dict(a.execution, len(a.plan.segments)),
                )
            self.last_result = {"mission_id": a.record["id"], "status": "emergency_stopped", "reason": reason}
        self._transition(MissionState.EMERGENCY_STOP, reason)

    def _on_estop_reset(self) -> None:
        if self.sm.state == MissionState.EMERGENCY_STOP:
            self._transition(MissionState.IDLE, "e-stop reset; previous mission is not resumed")
            self.active = None

    def _on_disarm(self, reason: str) -> None:
        if self.sm.state == MissionState.RUNNING:
            self.pause(f"rover disarmed ({reason})", by="system")

    # ------------------------------------------------------------------ monitor

    async def monitor(self) -> None:
        """Periodic progress persistence and localization-loss abort."""
        last_save = 0.0
        while True:
            await asyncio.sleep(0.5)
            a = self.active
            if a is None or a.executor is None:
                continue
            now = time.monotonic()
            if self.sm.state in (MissionState.RUNNING, MissionState.PAUSED) and now - last_save > self.cfg.mission.progress_save_interval_s:
                last_save = now
                try:
                    self._save_progress()
                except Exception:  # pragma: no cover
                    log.exception("progress save failed")
            if (
                self.sm.state == MissionState.PAUSED
                and a.pause_reason == "localization lost"
                and a.paused_at_mono is not None
                and now - a.paused_at_mono > self.cfg.localization.loss_abort_s
            ):
                if a.task:
                    a.task.cancel()
                self._finish(a, MissionState.FAILED, MissionStatus.FAILED,
                             f"localization lost for more than {self.cfg.localization.loss_abort_s:.0f} s")

    def snapshot(self, include_trace: bool = False) -> dict[str, Any]:
        a = self.active
        out: dict[str, Any] = {"state": self.sm.state.value, "last_result": self.last_result}
        if a is None:
            out["mission"] = None
            return out
        progress = a.executor.progress.to_dict(a.execution, len(a.plan.segments)) if a.executor else None
        out["mission"] = {
            "id": a.record["id"],
            "name": a.record["name"],
            "kind": a.record["kind"],
            "execution": a.execution,
            "pause_reason": a.pause_reason,
            "invalidated": a.invalidated,
            "message": a.message,
            "progress": progress,
            "plan_hash": a.plan.plan_hash,
            "summary": a.plan.summary.model_dump(mode="json"),
            "localization_note": a.plan.localization_note,
        }
        if include_trace and a.executor:
            out["mission"]["trace"] = a.executor.progress.trace
        return out
