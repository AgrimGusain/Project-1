"""Grid (no GPS) and GPS planning front-ends on top of the coverage planner."""

from __future__ import annotations

import math
from typing import Any

from shapely.geometry import Polygon

from rover.config import RoverConfig
from rover.core.enums import OperatingMode
from rover.planning.coverage import (
    PLANNER_VERSION,
    CoverageInput,
    PlanningError,
    TimingModel,
    compass_of,
    plan_coverage,
)
from rover.planning.geo import EnuFrame
from rover.planning.models import (
    CoveragePlan,
    GeoPoint,
    GpsPlanRequest,
    GridPlanRequest,
    PlanIssue,
    Point,
    to_m,
)

OPEN_LOOP_NOTE = (
    "No physical localization is available: this route is a COMMANDED path executed open-loop "
    "from timed motor commands and the calibration model. Displayed progress is an estimate of "
    "commanded motion, not proof that the rover physically covered the area."
)
SIM_NOTE = "Simulation: execution and coverage are simulated."
GPS_NOTE = (
    "GPS-guided execution: position comes from the GNSS receiver only while its fix is valid. "
    "Heading is derived from course-over-ground while moving; pivots are open-loop."
)


def _timing(cfg: RoverConfig, speed_mps: float) -> TimingModel:
    cal = cfg.calibration
    m = cfg.mission
    return TimingModel(
        speed_mps=speed_mps,
        arc_speed_mps=min(speed_mps, m.turn_output * cal.full_speed_mps),
        pivot_rate_dps=m.pivot_output * cal.full_pivot_dps,
        settle_s=m.settle_time_s,
    )


def _speed(cfg: RoverConfig, requested: float, issues: list[PlanIssue]) -> float:
    achievable = cfg.calibration.full_speed_mps * cfg.motion.max_output
    if requested > achievable:
        issues.append(
            PlanIssue(
                severity="warning", code="speed_clamped",
                message=f"requested {requested:.2f} m/s exceeds the achievable {achievable:.2f} m/s; clamped",
            )
        )
        return round(achievable, 4)
    return requested


def _check_mode(cfg: RoverConfig, mode: OperatingMode) -> None:
    if mode != cfg.mode:
        raise PlanningError(
            [
                PlanIssue(
                    severity="error", code="mode_mismatch",
                    message=f"plan requested for '{mode}' execution but the rover is running in '{cfg.mode}' mode. "
                    "The operating mode is fixed at startup and never switched automatically.",
                )
            ]
        )


def plan_grid(cfg: RoverConfig, req: GridPlanRequest) -> CoveragePlan:
    _check_mode(cfg, req.execution_mode)
    u = req.units
    length, width = to_m(req.length, u), to_m(req.width, u)
    spacing = to_m(req.lane_spacing, u)
    swath = to_m(req.swath_width, u) or spacing
    margin = to_m(req.margin, u) or 0.0
    radius = to_m(req.turn_radius, u)
    assert length and width and spacing
    if length > 2000 or width > 2000:
        raise PlanningError([PlanIssue(severity="error", code="too_large", message="grid dimensions are limited to 2000 m")])
    if 2 * margin >= min(length, width):
        raise PlanningError([PlanIssue(severity="error", code="margin_too_large", message="margin leaves no area to survey")])
    issues: list[PlanIssue] = []
    speed = _speed(cfg, req.speed_mps, issues)
    corners = {"near_left": (0.0, 0.0), "near_right": (width, 0.0), "far_left": (0.0, length), "far_right": (width, length)}
    lane_heading = 0.0 if req.lane_orientation == "along_length" else 90.0
    inp = CoverageInput(
        boundary=[(0.0, 0.0), (width, 0.0), (width, length), (0.0, length)],
        lane_spacing=spacing,
        swath=swath,
        margin=margin,
        lane_heading_deg=lane_heading,
        start_point=corners[req.start_corner],
        turn_strategy=req.turn_strategy,
        turn_radius=radius,
        initial_heading_deg=req.initial_heading_deg,
        max_lanes=cfg.mission.max_lanes,
        max_segments=cfg.mission.max_segments,
    )
    res = plan_coverage(inp, _timing(cfg, speed))
    issues += res.issues
    note = SIM_NOTE if cfg.is_simulation else OPEN_LOOP_NOTE
    if cfg.is_simulation and cfg.localization.provider == "none":
        note = SIM_NOTE + " " + OPEN_LOOP_NOTE
    return CoveragePlan(
        planner_version=PLANNER_VERSION,
        kind="grid",
        frame="grid_local",
        boundary=[Point(x=x, y=y) for x, y in inp.boundary],
        inner_boundary=res.inner,
        lane_heading_deg=lane_heading,
        start_pose=res.start_pose,
        segments=res.segments,
        waypoints=res.waypoints,
        summary=res.summary,
        issues=issues,
        valid=not any(i.severity == "error" for i in issues),
        parameters={**req.model_dump(mode="json"), "resolved_m": {
            "length": length, "width": width, "lane_spacing": spacing, "swath_width": swath,
            "margin": margin, "turn_radius": radius, "speed_mps": speed,
        }},
        plan_hash=res.plan_hash,
        localization_note=note,
    )


def auto_lane_heading(poly: Polygon) -> float:
    """Lanes parallel to the longest edge of the minimum rotated rectangle (fewest turns)."""
    rect = poly.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    best = (0.0, 0.0)
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length > best[0] + 1e-9:
            best = (length, compass_of(x1 - x0, y1 - y0) % 180.0)
    return round(best[1], 6)


def plan_gps(cfg: RoverConfig, req: GpsPlanRequest) -> CoveragePlan:
    _check_mode(cfg, req.execution_mode)
    if req.start_vertex >= len(req.boundary):
        raise PlanningError([PlanIssue(severity="error", code="bad_start_vertex", message="start_vertex is out of range")])
    u = req.units
    spacing = to_m(req.lane_spacing, u)
    swath = to_m(req.swath_width, u) or spacing
    margin = to_m(req.margin, u) or 0.0
    radius = to_m(req.turn_radius, u)
    assert spacing
    lat0 = sum(p.lat for p in req.boundary) / len(req.boundary)
    lon0 = sum(p.lon for p in req.boundary) / len(req.boundary)
    frame = EnuFrame(lat0, lon0, 0.0)
    pts = [frame.to_enu(p.lat, p.lon) for p in req.boundary]
    extent = max(math.hypot(a[0] - b[0], a[1] - b[1]) for a in pts for b in pts)
    if extent > cfg.localization.geo_max_extent_m:
        raise PlanningError(
            [PlanIssue(severity="error", code="area_too_large",
                       message=f"survey extent {extent:.0f} m exceeds the local-frame limit of {cfg.localization.geo_max_extent_m:.0f} m")]
        )
    issues: list[PlanIssue] = []
    speed = _speed(cfg, req.speed_mps, issues)
    poly = Polygon(pts)
    lane_heading = req.lane_heading_deg if req.lane_heading_deg is not None else (auto_lane_heading(poly) if poly.is_valid else 0.0)
    inp = CoverageInput(
        boundary=pts,
        lane_spacing=spacing,
        swath=swath,
        margin=margin,
        lane_heading_deg=lane_heading,
        start_point=pts[req.start_vertex],
        turn_strategy=req.turn_strategy,
        turn_radius=radius,
        initial_heading_deg=None,
        max_lanes=cfg.mission.max_lanes,
        max_segments=cfg.mission.max_segments,
    )
    res = plan_coverage(inp, _timing(cfg, speed))
    issues += res.issues
    acc = cfg.localization.gps.max_accuracy_m
    if spacing < 2 * acc:
        issues.append(
            PlanIssue(
                severity="warning", code="gps_accuracy",
                message=f"lane spacing {spacing:.2f} m is below twice the accepted GPS accuracy ({acc:.2f} m); "
                "adjacent lanes may overlap or leave gaps. An RTK receiver is needed for fine spacing.",
            )
        )

    def geo(p: Point) -> GeoPoint:
        lat, lon = frame.to_geodetic(p.x, p.y)
        return GeoPoint(lat=round(lat, 9), lon=round(lon, 9))

    for seg in res.segments:
        seg.geo_points = [geo(p) for p in seg.points]
    for wp in res.waypoints:
        g = geo(Point(x=wp.x, y=wp.y))
        wp.lat, wp.lon = g.lat, g.lon
    return CoveragePlan(
        planner_version=PLANNER_VERSION,
        kind="gps",
        frame="enu",
        origin=GeoPoint(lat=lat0, lon=lon0),
        boundary=[Point(x=round(x, 6), y=round(y, 6)) for x, y in pts],
        boundary_geo=list(req.boundary),
        inner_boundary=res.inner,
        lane_heading_deg=lane_heading,
        start_pose=res.start_pose,
        segments=res.segments,
        waypoints=res.waypoints,
        summary=res.summary,
        issues=issues,
        valid=not any(i.severity == "error" for i in issues),
        parameters={**req.model_dump(mode="json"), "resolved_m": {
            "lane_spacing": spacing, "swath_width": swath, "margin": margin,
            "turn_radius": radius, "speed_mps": speed, "lane_heading_deg": lane_heading,
        }},
        plan_hash=res.plan_hash,
        localization_note=(SIM_NOTE + " " + GPS_NOTE) if cfg.is_simulation else GPS_NOTE,
    )


def plan_from_request(cfg: RoverConfig, kind: str, params: dict[str, Any]) -> CoveragePlan:
    if kind == "grid":
        return plan_grid(cfg, GridPlanRequest.model_validate(params))
    if kind == "gps":
        return plan_gps(cfg, GpsPlanRequest.model_validate(params))
    raise PlanningError([PlanIssue(severity="error", code="bad_kind", message=f"unknown mission kind '{kind}'")])
