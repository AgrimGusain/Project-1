"""Mission executor: turns a coverage plan into motion-controller commands.

Execution modes (chosen by the manager from the localization provider):

* ``open_loop``  — no position source. Segments are executed from the calibration
  model; progress is integrated from the *applied* motor outputs. The estimated
  pose is a commanded-motion estimate, never a measurement.
* ``closed_loop`` — a local pose with heading is available (simulation truth or
  experimental odometry): pure-pursuit path following and closed-loop pivots.
* ``gnss`` — GNSS position (converted into the plan's ENU frame); heading only from
  course-over-ground while moving. Pivots are open-loop (no stationary heading).

The executor never owns lifecycle decisions: it asks the manager to pause or
raises :class:`MissionAbort`. Each tick renews a short motion lease, so if the
executor stops for any reason the controller stops the motors.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from rover.config import RoverConfig
from rover.core.clock import Clock
from rover.core.enums import ControlOwner
from rover.localization.base import LocalizationEstimate
from rover.motion.controller import CommandRejected, MotionController
from rover.planning.coverage import compass_of, wrap180
from rover.planning.geo import EnuFrame
from rover.planning.models import CoveragePlan, PathSegment, SegmentKind

log = logging.getLogger(__name__)

ExecMode = Literal["open_loop", "closed_loop", "gnss"]
TICK_S = 0.05


class MissionAbort(Exception):
    """Unrecoverable execution failure -> mission FAILED."""


@dataclass
class Pose:
    x: float
    y: float
    h: float


@dataclass
class ExecutorHooks:
    request_pause: Callable[[str], None]
    obstacle_blocked: Callable[[], str | None]
    localization: Callable[[], LocalizationEstimate]


@dataclass
class Progress:
    segment_index: int = 0
    segment_fraction: float = 0.0
    lanes_completed: int = 0
    lane_count: int = 0
    completed_weight: float = 0.0
    total_weight: float = 1.0
    distance_commanded_m: float = 0.0
    phase: str = "starting"
    elapsed_s: float = 0.0
    cross_track_m: float | None = None
    estimated_pose: Pose | None = None
    measured_pose: Pose | None = None
    trace: list[tuple[float, float]] = field(default_factory=list)

    @property
    def percent(self) -> float:
        return round(100.0 * min(1.0, self.completed_weight / max(self.total_weight, 1e-9)), 2)

    def to_dict(self, execution: ExecMode, segment_count: int) -> dict[str, Any]:
        def pose(p: Pose | None) -> dict[str, float] | None:
            return None if p is None else {"x": round(p.x, 3), "y": round(p.y, 3), "heading_deg": round(p.h % 360, 1)}

        return {
            "segment_index": self.segment_index,
            "segment_count": segment_count,
            "segment_fraction": round(self.segment_fraction, 3),
            "lanes_completed": self.lanes_completed,
            "lane_count": self.lane_count,
            "percent": self.percent,
            "distance_commanded_m": round(self.distance_commanded_m, 2),
            "phase": self.phase,
            "elapsed_s": round(self.elapsed_s, 1),
            "cross_track_m": None if self.cross_track_m is None else round(self.cross_track_m, 3),
            "execution": execution,
            # The open-loop pose is an estimate of commanded motion, not a measurement.
            "estimated_pose": pose(self.estimated_pose),
            "estimated_pose_kind": "commanded_dead_reckoning" if execution == "open_loop" else "planned_reference",
            "measured_pose": pose(self.measured_pose),
        }


class MissionExecutor:
    def __init__(
        self,
        plan: CoveragePlan,
        mode: ExecMode,
        config: RoverConfig,
        controller: MotionController,
        clock: Clock,
        holder_id: str,
        hooks: ExecutorHooks,
        start_index: int = 0,
    ) -> None:
        self.plan = plan
        self.mode = mode
        self.cfg = config
        self.mcfg = config.mission
        self.cal = config.calibration
        self.controller = controller
        self.clock = clock
        self.holder_id = holder_id
        self.hooks = hooks
        self.start_index = start_index
        self.frame = EnuFrame(plan.origin.lat, plan.origin.lon) if plan.origin else None
        self.running = asyncio.Event()
        self.running.set()
        self.pause_reason: str | None = None
        weights = [max(seg.estimated_duration_s, 0.01) for seg in plan.segments]
        self._weights = weights
        self.progress = Progress(lane_count=plan.summary.lane_count, total_weight=sum(weights))
        sp = plan.start_pose
        self.est = Pose(sp.x, sp.y, sp.heading_deg)
        self.progress.estimated_pose = self.est
        self._loss_since: float | None = None
        self._last_t = clock.now()
        self._last_trace: tuple[float, float] | None = None
        self.pause_count = 0

    # ------------------------------------------------------------------ control flow

    def pause(self, reason: str) -> None:
        self.pause_reason = reason
        self.pause_count += 1
        self.running.clear()

    def resume(self) -> None:
        self.pause_reason = None
        self._loss_since = None
        self.running.set()

    @property
    def speed_fraction(self) -> float:
        speed = float(self.plan.parameters.get("resolved_m", {}).get("speed_mps") or self.plan.summary.speed_mps)
        return min(speed / self.cal.full_speed_mps, self.cfg.motion.max_output)

    def _command(self, left: float, right: float) -> None:
        cap = self.cfg.motion.max_output
        peak = max(abs(left), abs(right), 1e-9)
        if peak > cap:
            left, right = left * cap / peak, right * cap / peak
        try:
            self.controller.command(ControlOwner.MISSION, self.holder_id, left, right, self.cfg.motion.mission_command_timeout_s)
        except CommandRejected as exc:
            if exc.code == "motion_not_permitted":
                self.hooks.request_pause(f"motion not permitted: {exc.message}")
            elif exc.code == "not_owner":
                raise MissionAbort(f"lost motion ownership: {exc.message}") from exc
            else:
                raise MissionAbort(exc.message) from exc

    def _stop(self) -> None:
        try:
            self.controller.command(ControlOwner.MISSION, self.holder_id, 0.0, 0.0, self.cfg.motion.mission_command_timeout_s)
        except CommandRejected:
            pass

    async def _tick(self) -> float:
        """Sleep one tick, integrate dead reckoning, handle pause. Returns mission-clock dt."""
        # Constant control resolution in mission time (scale is 1.0 on hardware).
        await asyncio.sleep(max(0.004, TICK_S / max(self.clock.scale, 1e-6)))
        if not self.running.is_set():
            self._stop()
            self.progress.phase = f"paused: {self.pause_reason}"
            while not self.running.is_set():
                self._stop()
                try:
                    await asyncio.wait_for(self.running.wait(), timeout=0.1)
                except TimeoutError:
                    pass
            self._last_t = self.clock.now()
        now = self.clock.now()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        self.progress.elapsed_s += dt
        self._integrate(dt)
        self._update_measured()
        return dt

    def _integrate(self, dt: float) -> None:
        left, right = self.controller.applied
        v_full = self.cal.full_speed_mps
        v = (left + right) / 2.0 * v_full
        omega = math.degrees((left - right) * v_full / self.cal.effective_track_m)
        hm = math.radians(self.est.h + omega * dt / 2)
        self.est.x += v * math.sin(hm) * dt
        self.est.y += v * math.cos(hm) * dt
        self.est.h = (self.est.h + omega * dt) % 360.0
        self.progress.distance_commanded_m += abs(v) * dt
        if self.mode == "open_loop":
            self._add_trace(self.est.x, self.est.y)

    def _add_trace(self, x: float, y: float) -> None:
        if self._last_trace is None or math.hypot(x - self._last_trace[0], y - self._last_trace[1]) >= 0.1:
            self._last_trace = (x, y)
            if len(self.progress.trace) < 20000:
                self.progress.trace.append((round(x, 3), round(y, 3)))

    def _pose_from(self, est: LocalizationEstimate) -> tuple[Pose | None, bool]:
        if not est.position_valid:
            return None, False
        if self.mode == "gnss":
            if self.frame is None or est.lat is None or est.lon is None:
                return None, False
            x, y = self.frame.to_enu(est.lat, est.lon)
        else:
            if est.x is None or est.y is None:
                return None, False
            x, y = est.x, est.y
        heading_ok = bool(est.heading_valid and est.heading_deg is not None)
        return Pose(x, y, est.heading_deg if heading_ok else float("nan")), heading_ok

    def _update_measured(self) -> None:
        if self.mode == "open_loop":
            return
        pose, _ = self._pose_from(self.hooks.localization())
        self.progress.measured_pose = pose
        if pose is not None:
            self._add_trace(pose.x, pose.y)

    def _measured(self) -> tuple[Pose | None, bool]:
        """Current measured pose; pauses the mission if localization is lost for too long."""
        pose, heading_ok = self._pose_from(self.hooks.localization())
        if pose is None:
            now = time.monotonic()
            if self._loss_since is None:
                self._loss_since = now
            elif now - self._loss_since >= self.cfg.localization.loss_pause_s:
                self.hooks.request_pause("localization lost")
            return None, False
        self._loss_since = None
        return pose, heading_ok

    def _check_obstacle(self, forward: bool) -> None:
        if not forward:
            return
        reason = self.hooks.obstacle_blocked()
        if reason:
            self.hooks.request_pause(f"obstacle: {reason}")

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        if self.mode == "gnss" and self.start_index == 0:
            await self._approach()
        segs = self.plan.segments
        self.progress.completed_weight = sum(self._weights[: self.start_index])
        self.progress.lanes_completed = sum(1 for s in segs[: self.start_index] if s.kind == SegmentKind.LANE)
        for i in range(self.start_index, len(segs)):
            seg = segs[i]
            self.progress.segment_index = i
            self.progress.segment_fraction = 0.0
            self.progress.phase = seg.kind.value
            await self._execute(seg)
            self._stop()
            await self._settle()
            self.progress.completed_weight += self._weights[i]
            self.progress.segment_fraction = 1.0
            if seg.kind == SegmentKind.LANE:
                self.progress.lanes_completed += 1
        self._stop()
        self.progress.phase = "complete"

    async def _settle(self) -> None:
        end = self.clock.now() + self.mcfg.settle_time_s
        while self.clock.now() < end:
            self._stop()
            await self._tick()

    async def _execute(self, seg: PathSegment) -> None:
        if seg.kind == SegmentKind.PIVOT:
            if self.mode == "closed_loop":
                await self._pivot_closed(seg)
            else:
                await self._pivot_open(seg)
        elif self.mode == "open_loop":
            if seg.kind == SegmentKind.ARC:
                await self._arc_open(seg)
            else:
                await self._straight_open(seg)
        else:
            await self._follow(seg)

    # ------------------------------------------------------------------ open loop

    async def _straight_open(self, seg: PathSegment) -> None:
        s = self.speed_fraction
        travelled = 0.0
        v_full = self.cal.full_speed_mps
        while travelled < seg.length_m:
            self._check_obstacle(True)
            self._command(s, s)
            dt = await self._tick()
            left, right = self.controller.applied
            travelled += max(0.0, (left + right) / 2.0) * v_full * dt
            self.progress.segment_fraction = min(1.0, travelled / max(seg.length_m, 1e-9))

    async def _pivot_open(self, seg: PathSegment) -> None:
        target = abs(seg.rotation_deg)
        p = self.mcfg.pivot_output
        sign = 1.0 if seg.rotation_deg > 0 else -1.0
        turned = 0.0
        v_full, track = self.cal.full_speed_mps, self.cal.effective_track_m
        while turned < target:
            remaining = target - turned
            out = p if remaining > 15 else max(0.3, p * remaining / 15)
            self._command(sign * out, -sign * out)
            dt = await self._tick()
            left, right = self.controller.applied
            turned += abs(math.degrees((left - right) * v_full / track)) * dt
            self.progress.segment_fraction = min(1.0, turned / max(target, 1e-9))

    async def _arc_open(self, seg: PathSegment) -> None:
        r = seg.turn_radius_m or 1.0
        target = abs(seg.rotation_deg)
        half = self.cal.effective_track_m / (2 * r)
        c = self.mcfg.turn_output
        outer, inner = c * (1 + half), c * (1 - half)
        left, right = (outer, inner) if seg.rotation_deg > 0 else (inner, outer)
        turned = 0.0
        v_full, track = self.cal.full_speed_mps, self.cal.effective_track_m
        while turned < target:
            self._check_obstacle(True)
            self._command(left, right)
            dt = await self._tick()
            al, ar = self.controller.applied
            turned += abs(math.degrees((al - ar) * v_full / track)) * dt
            self.progress.segment_fraction = min(1.0, turned / max(target, 1e-9))

    async def _rotate_open(self, rotation_deg: float) -> None:
        """Timed pivot from the calibration model (used when no stationary heading exists)."""
        from rover.planning.models import Point

        p = Point(x=self.est.x, y=self.est.y)
        seg = PathSegment(index=-1, kind=SegmentKind.PIVOT, start=p, end=p, heading_start_deg=0.0,
                          heading_end_deg=0.0, length_m=0.0, rotation_deg=rotation_deg, points=[p])
        await self._pivot_open(seg)
        self._stop()

    async def _approach(self) -> None:
        """Drive (GNSS-tracked) from the current position to the plan's start point."""
        from rover.planning.models import Point

        while True:
            pose, _ = self._measured()
            if pose is not None:
                break
            self._stop()
            await self._tick()
        sp = self.plan.start_pose
        dist = math.hypot(sp.x - pose.x, sp.y - pose.y)
        if dist <= self.mcfg.goal_tolerance_m * 2:
            return
        self.progress.phase = "approach"
        a, b = Point(x=pose.x, y=pose.y), Point(x=sp.x, y=sp.y)
        hd = compass_of(sp.x - pose.x, sp.y - pose.y)
        seg = PathSegment(index=-1, kind=SegmentKind.TRANSIT, start=a, end=b, heading_start_deg=hd, heading_end_deg=hd,
                          length_m=dist, points=[a, b])
        await self._follow(seg, label="approach")
        self._stop()
        await self._settle()

    # ------------------------------------------------------------------ closed loop

    async def _pivot_closed(self, seg: PathSegment) -> None:
        target = seg.heading_end_deg
        settle_ticks = 0
        while True:
            pose, heading_ok = self._measured()
            if pose is None or not heading_ok:
                self._stop()
                await self._tick()
                continue
            err = wrap180(target - pose.h)
            self.progress.segment_fraction = max(0.0, 1.0 - abs(err) / max(abs(seg.rotation_deg), 1e-9))
            if abs(err) <= self.mcfg.heading_tolerance_deg:
                settle_ticks += 1
                self._stop()
                if settle_ticks >= 3:
                    return
            else:
                settle_ticks = 0
                out = max(0.3, min(self.mcfg.pivot_output, self.mcfg.pivot_output * abs(err) / 25.0))
                sign = 1.0 if err > 0 else -1.0
                self._command(sign * out, -sign * out)
            await self._tick()

    async def _follow(self, seg: PathSegment, label: str | None = None) -> None:
        name = label or seg.kind.value
        pts = [(p.x, p.y) for p in seg.points]
        cum = [0.0]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
        total = cum[-1]
        if total < 1e-6:
            return
        s_base = self.speed_fraction if seg.kind != SegmentKind.ARC else min(self.speed_fraction, self.mcfg.turn_output)
        idx = 0
        stalled_since: float | None = None
        best_progress = -1.0
        pauses = self.pause_count
        while True:
            if self.pause_count != pauses:
                pauses, stalled_since = self.pause_count, None
            self._check_obstacle(True)
            pose, heading_ok = self._measured()
            if pose is None:
                self._stop()
                await self._tick()
                continue
            # project onto the polyline, searching forward from the last index
            best = (float("inf"), idx, 0.0)
            for k in range(idx, min(idx + 40, len(pts) - 1)):
                (x0, y0), (x1, y1) = pts[k], pts[k + 1]
                dx, dy = x1 - x0, y1 - y0
                ln2 = dx * dx + dy * dy
                t = 0.0 if ln2 == 0 else max(0.0, min(1.0, ((pose.x - x0) * dx + (pose.y - y0) * dy) / ln2))
                px, py = x0 + t * dx, y0 + t * dy
                d = math.hypot(pose.x - px, pose.y - py)
                if d < best[0]:
                    best = (d, k, t)
            _, idx, t = best
            seg_len = cum[idx + 1] - cum[idx]
            s_proj = cum[idx] + t * seg_len
            # beyond the end of the last piece along its direction?
            (xa, ya), (xb, yb) = pts[-2], pts[-1]
            along_end = ((pose.x - xb) * (xb - xa) + (pose.y - yb) * (yb - ya)) / max(math.hypot(xb - xa, yb - ya), 1e-9)
            remaining = total - s_proj
            self.progress.segment_fraction = min(1.0, s_proj / total)
            # signed cross-track error relative to the current piece
            (x0, y0), (x1, y1) = pts[idx], pts[idx + 1]
            piece = max(math.hypot(x1 - x0, y1 - y0), 1e-9)
            self.progress.cross_track_m = ((pose.x - x0) * (y1 - y0) - (pose.y - y0) * (x1 - x0)) / piece
            if remaining <= self.mcfg.goal_tolerance_m or along_end >= 0:
                return
            # stall detection (e.g. pushing against something the sensors miss)
            now = self.clock.now()
            if s_proj > best_progress + 0.02:
                best_progress, stalled_since = s_proj, None
            elif stalled_since is None:
                stalled_since = now
            elif now - stalled_since > 15.0 and self.running.is_set():
                raise MissionAbort(f"no progress along segment {seg.index} for 15 s (stalled or blocked)")
            # look-ahead target
            target_s = min(total, s_proj + self.mcfg.lookahead_m)
            j = idx
            while j < len(cum) - 2 and cum[j + 1] < target_s:
                j += 1
            frac = (target_s - cum[j]) / max(cum[j + 1] - cum[j], 1e-9)
            tx = pts[j][0] + frac * (pts[j + 1][0] - pts[j][0])
            ty = pts[j][1] + frac * (pts[j + 1][1] - pts[j][1])
            speed = s_base * max(0.35, min(1.0, remaining / 0.4))
            if not heading_ok:
                # GNSS without course-over-ground yet: creep straight to acquire heading.
                self.progress.phase = f"{name}: acquiring heading"
                creep = min(self.mcfg.approach_output, speed)
                self._command(creep, creep)
            else:
                self.progress.phase = name
                err = wrap180(compass_of(tx - pose.x, ty - pose.y) - pose.h)
                if abs(err) > 45 and self.mode == "closed_loop":
                    out = min(self.mcfg.pivot_output, 0.5)
                    sign = 1.0 if err > 0 else -1.0
                    self._command(sign * out, -sign * out)
                elif abs(err) > 60 and self.mode == "gnss":
                    # Course-over-ground is far off: stop, pivot open-loop, re-acquire heading.
                    self._stop()
                    await self._settle()
                    await self._rotate_open(err)
                    continue
                else:
                    turn = max(-1.0, min(1.0, err / 30.0)) * speed * 0.8
                    self._command(speed + turn, speed - turn)
            await self._tick()
