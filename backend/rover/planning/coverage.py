"""Boustrophedon coverage planner for arbitrary simple polygons.

Algorithm
---------
1. Validate the boundary (simple, non-degenerate) and inset it by the margin.
2. Rotate into a sweep frame (u along the lanes, v across them) and place lanes
   at ``swath/2`` from the inner boundary and every ``lane_spacing`` after that;
   the final lane is clamped so it never leaves the inner boundary.
3. Intersect each lane line with the inner polygon. Concave shapes produce several
   intervals per lane; intervals are grouped into *cells* (a simple boustrophedon
   cell decomposition: an interval continues a cell when it overlaps exactly one
   interval of the previous lane and vice versa).
4. Visit cells greedily (nearest entry first, deterministic tie-breaks). Inside a
   cell lanes alternate direction.
5. Connect lanes with pivot turns (pivot - shift - pivot) or Dubins arc turns of the
   configured radius; connect cells with transits.
6. Verify every non-lane segment stays inside the *outer* boundary. Violations
   are reported as errors and make the plan invalid (never silently accepted).

The result is deterministic for identical inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import shapely
from shapely.geometry import LineString, Point as ShpPoint, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from rover.planning import dubins
from rover.planning.models import (
    PathSegment,
    PlanIssue,
    PlanSummary,
    Point,
    Pose2D,
    SegmentKind,
    Waypoint,
)

PLANNER_VERSION = "1.0.0"
_EPS = 1e-9
MIN_LANE_LENGTH_M = 0.05


class PlanningError(ValueError):
    def __init__(self, issues: list[PlanIssue]) -> None:
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


def _err(code: str, message: str) -> PlanningError:
    return PlanningError([PlanIssue(severity="error", code=code, message=message)])


def wrap180(a: float) -> float:
    """Wrap to (-180, 180]."""
    a = math.fmod(a + 180.0, 360.0)
    if a <= 0:
        a += 360.0
    return a - 180.0


def compass_of(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


def compass_to_theta(h: float) -> float:
    return math.radians(90.0 - h)


def theta_to_compass(th: float) -> float:
    return (90.0 - math.degrees(th)) % 360.0


@dataclass
class TimingModel:
    speed_mps: float
    arc_speed_mps: float
    pivot_rate_dps: float
    settle_s: float


@dataclass
class CoverageInput:
    boundary: list[tuple[float, float]]
    lane_spacing: float
    swath: float
    margin: float
    lane_heading_deg: float
    start_point: tuple[float, float]
    turn_strategy: str = "pivot"
    turn_radius: float | None = None
    initial_heading_deg: float | None = None
    max_lanes: int = 400
    max_segments: int = 5000


@dataclass
class CoverageResult:
    segments: list[PathSegment]
    waypoints: list[Waypoint]
    summary: PlanSummary
    issues: list[PlanIssue]
    inner: list[list[Point]]
    start_pose: Pose2D
    plan_hash: str


@dataclass
class _Lane:
    order: int
    v: float
    u0: float
    u1: float


@dataclass
class _Cell:
    index: int
    lanes: list[_Lane] = field(default_factory=list)


def validate_polygon(boundary: list[tuple[float, float]]) -> Polygon:
    if len(boundary) < 3:
        raise _err("too_few_vertices", "boundary needs at least 3 vertices")
    for x, y in boundary:
        if not (math.isfinite(x) and math.isfinite(y)):
            raise _err("non_finite", "boundary contains non-finite coordinates")
    poly = Polygon(boundary)
    if not poly.is_valid:
        raise _err("invalid_polygon", f"boundary polygon is invalid: {explain_validity(poly)}")
    if poly.area < 0.01:
        raise _err("degenerate_polygon", "boundary area is below 0.01 m^2")
    return poly


def _polys(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def _rings(geom: BaseGeometry) -> list[list[Point]]:
    return [[Point(x=round(x, 6), y=round(y, 6)) for x, y in p.exterior.coords] for p in _polys(geom)]


def _intervals(geom: BaseGeometry, line: LineString, d: tuple[float, float]) -> list[tuple[float, float]]:
    inter = geom.intersection(line)
    pieces: list[tuple[float, float]] = []

    def visit(g: BaseGeometry) -> None:
        if g.is_empty:
            return
        if isinstance(g, LineString):
            coords = list(g.coords)
            us = [x * d[0] + y * d[1] for x, y in coords]
            pieces.append((min(us), max(us)))
        else:
            for sub in getattr(g, "geoms", []):
                visit(sub)

    visit(inter)
    pieces.sort()
    merged: list[tuple[float, float]] = []
    for a, b in pieces:
        if merged and a <= merged[-1][1] + 1e-7:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 1e-6


class _Builder:
    def __init__(self, outer: Polygon, timing: TimingModel, strategy: str, radius: float | None) -> None:
        self.outer_tol = outer.buffer(1e-6)
        self.timing = timing
        self.strategy = strategy
        self.radius = radius
        self.segments: list[PathSegment] = []
        self.x = 0.0
        self.y = 0.0
        self.h = 0.0

    def set_pose(self, x: float, y: float, h: float) -> None:
        self.x, self.y, self.h = x, y, h % 360.0

    def _append(self, **kw: object) -> PathSegment:
        pts = kw["points"]
        assert isinstance(pts, list)
        geom = LineString([(p.x, p.y) for p in pts]) if len(pts) > 1 else ShpPoint(pts[0].x, pts[0].y)
        seg = PathSegment(index=len(self.segments), within_boundary=bool(self.outer_tol.covers(geom)), **kw)  # type: ignore[arg-type]
        self.segments.append(seg)
        return seg

    def _pt(self, x: float, y: float) -> Point:
        return Point(x=round(x, 6), y=round(y, 6))

    def pivot_to(self, heading: float) -> None:
        rot = wrap180(heading - self.h)
        if abs(rot) < 0.05:
            self.h = heading % 360.0
            return
        p = self._pt(self.x, self.y)
        self._append(
            kind=SegmentKind.PIVOT, start=p, end=p, heading_start_deg=round(self.h, 4),
            heading_end_deg=round(heading % 360.0, 4), length_m=0.0, rotation_deg=round(rot, 4), points=[p],
            estimated_duration_s=abs(rot) / self.timing.pivot_rate_dps + self.timing.settle_s,
        )
        self.h = heading % 360.0

    def straight(self, kind: SegmentKind, x: float, y: float, lane: int | None = None, cell: int | None = None) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        if dist < 1e-6:
            return
        heading = compass_of(x - self.x, y - self.y)
        self.pivot_to(heading)
        a, b = self._pt(self.x, self.y), self._pt(x, y)
        self._append(
            kind=kind, lane_index=lane, cell_index=cell, start=a, end=b, heading_start_deg=round(heading, 4),
            heading_end_deg=round(heading, 4), length_m=round(dist, 6), points=[a, b],
            estimated_duration_s=dist / self.timing.speed_mps,
        )
        self.x, self.y = x, y

    def connect(self, x: float, y: float, heading: float, kind: SegmentKind, cell: int | None) -> None:
        """Move to pose (x, y, heading) with the configured turn strategy."""
        if self.strategy == "arc" and self.radius:
            self._dubins(x, y, heading, kind, cell)
        else:
            self.straight(kind, x, y, cell=cell)
            self.pivot_to(heading)

    def _dubins(self, x: float, y: float, heading: float, kind: SegmentKind, cell: int | None) -> None:
        r = float(self.radius or 0.0)
        q = (self.x, self.y, compass_to_theta(self.h))
        path = dubins.shortest_path(q, (x, y, compass_to_theta(heading)), r)
        for prim, amount in zip(path.word, path.params):
            if amount < 1e-9:
                continue
            if prim == "S":
                q2 = dubins.step(q, "S", amount * r, r)
                a, b = self._pt(q[0], q[1]), self._pt(q2[0], q2[1])
                hd = theta_to_compass(q[2])
                self._append(
                    kind=SegmentKind.SHIFT if kind == SegmentKind.SHIFT else SegmentKind.TRANSIT, cell_index=cell,
                    start=a, end=b, heading_start_deg=round(hd, 4), heading_end_deg=round(hd, 4),
                    length_m=round(amount * r, 6), points=[a, b], estimated_duration_s=amount * r / self.timing.speed_mps,
                )
            else:
                center = dubins.turn_center(q, prim, r)
                n = max(2, math.ceil(math.degrees(amount) / 5.0))
                pts = [self._pt(q[0], q[1])]
                for i in range(1, n + 1):
                    qi = dubins.step(q, prim, amount * i / n, r)
                    pts.append(self._pt(qi[0], qi[1]))
                q2 = dubins.step(q, prim, amount, r)
                rot = -math.degrees(amount) if prim == "L" else math.degrees(amount)
                self._append(
                    kind=SegmentKind.ARC, cell_index=cell, start=pts[0], end=pts[-1],
                    heading_start_deg=round(theta_to_compass(q[2]), 4), heading_end_deg=round(theta_to_compass(q2[2]), 4),
                    length_m=round(amount * r, 6), rotation_deg=round(rot, 4), turn_radius_m=r,
                    arc_center=self._pt(*center), points=pts, estimated_duration_s=amount * r / self.timing.arc_speed_mps,
                )
            q = dubins.step(q, prim, amount if prim != "S" else amount * r, r)
        self.set_pose(x, y, heading)


def plan_coverage(inp: CoverageInput, timing: TimingModel) -> CoverageResult:
    issues: list[PlanIssue] = []
    if inp.lane_spacing <= 0 or inp.swath <= 0:
        raise _err("invalid_spacing", "lane spacing and swath width must be positive")
    if inp.margin < 0:
        raise _err("invalid_margin", "margin cannot be negative")
    if inp.turn_strategy == "arc" and not (inp.turn_radius and inp.turn_radius > 0):
        raise _err("missing_radius", "arc turns require a positive turn radius")
    outer = validate_polygon(inp.boundary)
    inner = outer.buffer(-inp.margin, join_style="mitre", mitre_limit=5.0) if inp.margin > 0 else outer
    if inner.is_empty or inner.area < 1e-4:
        raise _err("margin_too_large", "the margin leaves no area to survey")
    if inp.lane_spacing > inp.swath + 1e-9:
        issues.append(
            PlanIssue(
                severity="warning", code="coverage_gaps",
                message=f"lane spacing {inp.lane_spacing:.3f} m exceeds swath {inp.swath:.3f} m: strips between lanes are not covered",
            )
        )

    h = inp.lane_heading_deg % 360.0
    d = (math.sin(math.radians(h)), math.cos(math.radians(h)))  # along lanes
    vv = (-d[1], d[0])  # across lanes
    coords = [c for p in _polys(inner) for c in p.exterior.coords]
    us = [x * d[0] + y * d[1] for x, y in coords]
    vs = [x * vv[0] + y * vv[1] for x, y in coords]
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    width = vmax - vmin
    s = inp.swath
    # Sweep starts from the side nearest the requested start point; lanes are laid
    # out from that side so any narrower final gap is on the far side.
    sv = inp.start_point[0] * vv[0] + inp.start_point[1] * vv[1]
    from_max = abs(sv - vmax) < abs(sv - vmin)
    if width <= s:
        lane_vs = [(vmin + vmax) / 2.0]
    else:
        n = math.ceil((width - s) / inp.lane_spacing - 1e-9) + 1
        if n > inp.max_lanes:
            raise _err("too_many_lanes", f"{n} lanes exceed the limit of {inp.max_lanes}; increase lane spacing")
        lane_vs = []
        for k in range(n):
            off = min(s / 2.0 + k * inp.lane_spacing, width - s / 2.0)
            v = vmax - off if from_max else vmin + off
            if not lane_vs or abs(v - lane_vs[-1]) > 1e-6:
                lane_vs.append(v)

    lanes_by_order: list[list[_Lane]] = []
    skipped = 0
    for order, v in enumerate(lane_vs):
        p0 = (umin - 1.0) * d[0] + v * vv[0], (umin - 1.0) * d[1] + v * vv[1]
        p1 = (umax + 1.0) * d[0] + v * vv[0], (umax + 1.0) * d[1] + v * vv[1]
        ivs = _intervals(inner, LineString([p0, p1]), d)
        row = []
        for a, b in ivs:
            if b - a < MIN_LANE_LENGTH_M:
                skipped += 1
                continue
            row.append(_Lane(order, v, a, b))
        lanes_by_order.append(row)
    if skipped:
        issues.append(PlanIssue(severity="info", code="short_lanes_skipped", message=f"{skipped} lane pieces shorter than {MIN_LANE_LENGTH_M} m were skipped"))
    if not any(lanes_by_order):
        raise _err("no_lanes", "no survey lanes fit inside the boundary")

    # Cell decomposition.
    cells: list[_Cell] = []
    prev: list[tuple[_Lane, _Cell]] = []
    for row in lanes_by_order:
        current: list[tuple[_Lane, _Cell]] = []
        for lane in row:
            cand = [(pl, pc) for pl, pc in prev if _overlaps((pl.u0, pl.u1), (lane.u0, lane.u1))]
            cell: _Cell | None = None
            if len(cand) == 1:
                pl, pc = cand[0]
                if sum(1 for other in row if _overlaps((pl.u0, pl.u1), (other.u0, other.u1))) == 1:
                    cell = pc
            if cell is None:
                cell = _Cell(len(cells))
                cells.append(cell)
            cell.lanes.append(lane)
            current.append((lane, cell))
        prev = current

    def lane_point(lane: _Lane, u: float) -> tuple[float, float]:
        return u * d[0] + lane.v * vv[0], u * d[1] + lane.v * vv[1]

    # Greedy cell ordering.
    remaining = list(cells)
    pos = inp.start_point
    ordered: list[tuple[_Cell, bool, int]] = []
    while remaining:
        best: tuple[float, int, _Cell, bool, int] | None = None
        for cell in remaining:
            for rev in (False, True):
                first = cell.lanes[-1] if rev else cell.lanes[0]
                for direction in (1, -1):
                    u = first.u0 if direction == 1 else first.u1
                    ex, ey = lane_point(first, u)
                    dist = math.hypot(ex - pos[0], ey - pos[1])
                    key = (round(dist, 9), cell.index)
                    if best is None or key < (round(best[0], 9), best[1]):
                        best = (dist, cell.index, cell, rev, direction)
        assert best is not None
        _, _, cell, rev, direction = best
        ordered.append((cell, rev, direction))
        remaining.remove(cell)
        lanes = list(reversed(cell.lanes)) if rev else cell.lanes
        last = lanes[-1]
        last_dir = direction * (1 if (len(lanes) - 1) % 2 == 0 else -1)
        pos = lane_point(last, last.u1 if last_dir == 1 else last.u0)

    # Build segments.
    builder = _Builder(outer, timing, inp.turn_strategy, inp.turn_radius)
    waypoints: list[Waypoint] = []
    first_cell, first_rev, first_dir = ordered[0]
    first_lane = (first_cell.lanes[-1] if first_rev else first_cell.lanes[0])
    sx, sy = lane_point(first_lane, first_lane.u0 if first_dir == 1 else first_lane.u1)
    first_heading = h if first_dir == 1 else (h + 180.0) % 360.0
    start_heading = first_heading if inp.initial_heading_deg is None else inp.initial_heading_deg % 360.0
    builder.set_pose(sx, sy, start_heading)
    start_pose = Pose2D(x=round(sx, 6), y=round(sy, 6), heading_deg=round(start_heading, 4))
    waypoints.append(Waypoint(index=0, kind="start", x=start_pose.x, y=start_pose.y, heading_deg=start_heading, segment_index=0))

    lane_counter = 0
    for ci, (cell, rev, direction) in enumerate(ordered):
        lanes = list(reversed(cell.lanes)) if rev else cell.lanes
        for j, lane in enumerate(lanes):
            ldir = direction * (1 if j % 2 == 0 else -1)
            ua, ub = (lane.u0, lane.u1) if ldir == 1 else (lane.u1, lane.u0)
            ax, ay = lane_point(lane, ua)
            bx, by = lane_point(lane, ub)
            lane_heading = h if ldir == 1 else (h + 180.0) % 360.0
            if math.hypot(ax - builder.x, ay - builder.y) > 1e-6:
                kind = SegmentKind.SHIFT if j > 0 else SegmentKind.TRANSIT
                builder.connect(ax, ay, lane_heading, kind, cell.index)
            else:
                builder.pivot_to(lane_heading)
            waypoints.append(
                Waypoint(index=len(waypoints), kind="lane_start", x=round(ax, 6), y=round(ay, 6), heading_deg=lane_heading,
                         segment_index=len(builder.segments), lane_index=lane_counter)
            )
            builder.straight(SegmentKind.LANE, bx, by, lane=lane_counter, cell=cell.index)
            waypoints.append(
                Waypoint(index=len(waypoints), kind="lane_end", x=round(bx, 6), y=round(by, 6), heading_deg=lane_heading,
                         segment_index=len(builder.segments) - 1, lane_index=lane_counter)
            )
            lane_counter += 1
            if len(builder.segments) > inp.max_segments:
                raise _err("too_many_segments", f"route exceeds {inp.max_segments} segments")

    if len(ordered) > 1:
        issues.append(PlanIssue(severity="info", code="multi_cell", message=f"boundary split into {len(ordered)} cells (concave shape)"))
    segs = builder.segments
    waypoints.append(Waypoint(index=len(waypoints), kind="end", x=round(builder.x, 6), y=round(builder.y, 6), heading_deg=builder.h, segment_index=len(segs) - 1))

    outside = [sg for sg in segs if not sg.within_boundary]
    if outside:
        kinds = sorted({sg.kind.value for sg in outside})
        hint = (
            f" Arc turns need a margin of at least the turn radius ({inp.turn_radius:.2f} m) or use pivot turns."
            if inp.turn_strategy == "arc" else " Transits between separated regions cross outside the boundary."
        )
        issues.append(
            PlanIssue(severity="error", code="leaves_boundary", message=f"{len(outside)} {'/'.join(kinds)} segment(s) leave the survey boundary.{hint}")
        )
    if inp.turn_strategy == "arc" and inp.turn_radius and inp.lane_spacing < 2 * inp.turn_radius - 1e-9:
        issues.append(
            PlanIssue(severity="warning", code="loop_turns", message="lane spacing is below twice the turn radius; looping (bulb) turns were generated")
        )

    lane_len = sum(sg.length_m for sg in segs if sg.kind == SegmentKind.LANE)
    turn_len = sum(sg.length_m for sg in segs if sg.kind in (SegmentKind.ARC, SegmentKind.SHIFT))
    transit_len = sum(sg.length_m for sg in segs if sg.kind == SegmentKind.TRANSIT)
    lane_lines = [LineString([(sg.start.x, sg.start.y), (sg.end.x, sg.end.y)]) for sg in segs if sg.kind == SegmentKind.LANE]
    covered = shapely.union_all([ln.buffer(s / 2.0, cap_style="flat") for ln in lane_lines]).intersection(outer).area if lane_lines else 0.0
    summary = PlanSummary(
        lane_count=lane_counter,
        cell_count=len(ordered),
        segment_count=len(segs),
        total_length_m=round(lane_len + turn_len + transit_len, 3),
        lane_length_m=round(lane_len, 3),
        turn_length_m=round(turn_len, 3),
        transit_length_m=round(transit_len, 3),
        pivot_count=sum(1 for sg in segs if sg.kind == SegmentKind.PIVOT),
        total_rotation_deg=round(sum(abs(sg.rotation_deg) for sg in segs), 2),
        estimated_duration_s=round(sum(sg.estimated_duration_s for sg in segs), 1),
        area_m2=round(outer.area, 4),
        inner_area_m2=round(inner.area, 4),
        covered_area_m2=round(covered, 4),
        nominal_coverage_ratio=round(covered / outer.area, 4) if outer.area else 0.0,
        lane_spacing_m=inp.lane_spacing,
        swath_width_m=s,
        speed_mps=timing.speed_mps,
    )
    canonical = json.dumps(
        [[sg.kind.value, sg.start.x, sg.start.y, sg.end.x, sg.end.y, sg.rotation_deg] for sg in segs], separators=(",", ":")
    )
    plan_hash = hashlib.sha256(f"{PLANNER_VERSION}|{canonical}".encode()).hexdigest()[:20]
    return CoverageResult(segs, waypoints, summary, issues, _rings(inner), start_pose, plan_hash)
