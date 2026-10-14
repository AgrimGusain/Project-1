"""Typed planning inputs and outputs.

Coordinate convention (docs/COORDINATES.md):
* local frame, metres: x = right/east, y = forward/north;
* heading: compass degrees, 0 = +y, 90 = +x, clockwise positive;
* grid surveys: origin (0, 0) at the near-left corner of the survey rectangle,
  ``width`` along +x and ``length`` along +y. "Near" is the edge the operator
  stands at; "far" is the opposite edge.
* GPS surveys: local ENU frame (x east, y north) centred on the boundary centroid.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rover.core.enums import OperatingMode

UNIT_SCALE = {"m": 1.0, "cm": 0.01, "mm": 0.001}


class Units(StrEnum):
    M = "m"
    CM = "cm"
    MM = "mm"


def to_m(value: float | None, units: Units | str) -> float | None:
    return None if value is None else float(value) * UNIT_SCALE[str(units)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(BaseModel):
    x: float
    y: float


class GeoPoint(_Strict):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class Pose2D(BaseModel):
    x: float
    y: float
    heading_deg: float


class SegmentKind(StrEnum):
    LANE = "lane"  # survey lane (sensor sweep)
    ARC = "arc"  # constant-radius turn
    PIVOT = "pivot"  # rotate in place
    SHIFT = "shift"  # straight move between adjacent lanes
    TRANSIT = "transit"  # straight move between disconnected cells


class PathSegment(BaseModel):
    index: int
    kind: SegmentKind
    lane_index: int | None = None
    cell_index: int | None = None
    start: Point
    end: Point
    heading_start_deg: float
    heading_end_deg: float
    length_m: float
    rotation_deg: float = 0.0  # signed, clockwise positive
    turn_radius_m: float | None = None
    arc_center: Point | None = None
    points: list[Point]
    geo_points: list[GeoPoint] | None = None
    within_boundary: bool = True
    estimated_duration_s: float = 0.0


class Waypoint(BaseModel):
    index: int
    kind: Literal["start", "lane_start", "lane_end", "end"]
    x: float
    y: float
    heading_deg: float
    segment_index: int
    lane_index: int | None = None
    lat: float | None = None
    lon: float | None = None


class PlanIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str


class PlanSummary(BaseModel):
    lane_count: int
    cell_count: int
    segment_count: int
    total_length_m: float
    lane_length_m: float
    turn_length_m: float
    transit_length_m: float
    pivot_count: int
    total_rotation_deg: float
    estimated_duration_s: float
    area_m2: float
    inner_area_m2: float
    covered_area_m2: float
    nominal_coverage_ratio: float
    lane_spacing_m: float
    swath_width_m: float
    speed_mps: float


class CoveragePlan(BaseModel):
    planner_version: str
    kind: Literal["grid", "gps"]
    frame: Literal["grid_local", "enu"]
    origin: GeoPoint | None = None
    boundary: list[Point]
    boundary_geo: list[GeoPoint] | None = None
    inner_boundary: list[list[Point]]
    lane_heading_deg: float
    start_pose: Pose2D
    segments: list[PathSegment]
    waypoints: list[Waypoint]
    summary: PlanSummary
    issues: list[PlanIssue]
    valid: bool
    parameters: dict[str, Any]
    plan_hash: str
    # Honest labelling for execution without physical localization.
    localization_note: str = ""


class GridPlanRequest(_Strict):
    units: Units = Units.M
    length: float = Field(..., gt=0, description="Extent along +y (forward from the near edge)")
    width: float = Field(..., gt=0, description="Extent along +x (to the right)")
    lane_spacing: float = Field(..., gt=0)
    swath_width: float | None = Field(None, gt=0, description="Sensor footprint width; default = lane spacing")
    margin: float = Field(0.0, ge=0)
    start_corner: Literal["near_left", "near_right", "far_left", "far_right"] = "near_left"
    lane_orientation: Literal["along_length", "along_width"] = "along_length"
    initial_heading_deg: float | None = Field(None, ge=-360, le=360)
    turn_strategy: Literal["pivot", "arc"] = "pivot"
    turn_radius: float | None = Field(None, gt=0)
    speed_mps: float = Field(0.2, gt=0, le=3.0)
    execution_mode: OperatingMode

    @field_validator("length", "width")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v == float("inf"):
            raise ValueError("must be finite")
        return v


class GpsPlanRequest(_Strict):
    boundary: list[GeoPoint] = Field(..., min_length=3, max_length=200)
    units: Units = Units.M
    lane_spacing: float = Field(..., gt=0)
    swath_width: float | None = Field(None, gt=0)
    margin: float = Field(0.0, ge=0)
    lane_heading_deg: float | None = Field(None, ge=0, lt=360, description="None = auto (fewest lanes)")
    start_vertex: int = Field(0, ge=0)
    turn_strategy: Literal["pivot", "arc"] = "pivot"
    turn_radius: float | None = Field(None, gt=0)
    speed_mps: float = Field(0.2, gt=0, le=3.0)
    execution_mode: OperatingMode
