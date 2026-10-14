import math

import pytest

from rover.config import RoverConfig
from rover.planning import dubins
from rover.planning.coverage import CoverageInput, PlanningError, TimingModel, plan_coverage, wrap180
from rover.planning.geo import EnuFrame, haversine_m
from rover.planning.models import GpsPlanRequest, GridPlanRequest, SegmentKind
from rover.planning.service import plan_gps, plan_grid

CFG = RoverConfig()
TIMING = TimingModel(speed_mps=0.2, arc_speed_mps=0.15, pivot_rate_dps=60, settle_s=0.2)


def grid(**kw):
    base = dict(length=4, width=2, lane_spacing=0.5, execution_mode="simulation")
    base.update(kw)
    return plan_grid(CFG, GridPlanRequest(**base))


def lanes(plan):
    return [s for s in plan.segments if s.kind == SegmentKind.LANE]


def test_rectangle_lanes_alternate_and_stay_inside():
    p = grid(margin=0.1)
    assert p.valid
    ls = lanes(p)
    assert len(ls) == p.summary.lane_count == 4
    headings = [round(s.heading_start_deg) % 360 for s in ls]
    assert headings == [0, 180, 0, 180]
    for s in p.segments:
        for pt in s.points:
            assert -1e-6 <= pt.x <= 2 + 1e-6 and -1e-6 <= pt.y <= 4 + 1e-6
    # lanes are inside the margin
    for s in ls:
        assert 0.1 - 1e-6 <= s.start.x <= 1.9 + 1e-6
        assert {round(s.start.y, 6), round(s.end.y, 6)} == {0.1, 3.9}


def test_route_is_continuous():
    p = grid(margin=0.1, turn_strategy="pivot")
    for a, b in zip(p.segments, p.segments[1:]):
        assert math.isclose(a.end.x, b.start.x, abs_tol=1e-5) and math.isclose(a.end.y, b.start.y, abs_tol=1e-5)
        assert abs(wrap180(a.heading_end_deg - b.heading_start_deg)) < 1e-3


def test_deterministic_hash():
    assert grid(margin=0.2).plan_hash == grid(margin=0.2).plan_hash
    assert grid(margin=0.2).plan_hash != grid(margin=0.25).plan_hash


def test_units_conversion_equivalent():
    a = grid(length=4, width=2, lane_spacing=0.5, margin=0.1, units="m")
    b = grid(length=400, width=200, lane_spacing=50, margin=10, units="cm")
    c = grid(length=4000, width=2000, lane_spacing=500, margin=100, units="mm")
    assert a.plan_hash == b.plan_hash == c.plan_hash


@pytest.mark.parametrize(
    "corner,start",
    [("near_left", (0.35, 0.1)), ("near_right", (1.65, 0.1)), ("far_left", (0.35, 3.9)), ("far_right", (1.65, 3.9))],
)
def test_start_corner(corner, start):
    p = grid(margin=0.1, start_corner=corner)
    assert (round(p.start_pose.x, 6), round(p.start_pose.y, 6)) == start


def test_lane_orientation_along_width():
    p = grid(margin=0.0, lane_orientation="along_width")
    assert all(round(s.heading_start_deg) % 180 == 90 for s in lanes(p))


def test_initial_heading_inserts_pivot():
    p = grid(initial_heading_deg=90)
    assert p.segments[0].kind == SegmentKind.PIVOT
    assert math.isclose(p.segments[0].rotation_deg, -90)


def test_coverage_and_duration_reported():
    p = grid(margin=0.0)
    s = p.summary
    assert s.area_m2 == pytest.approx(8.0)
    assert s.nominal_coverage_ratio == pytest.approx(1.0, abs=1e-3)
    assert s.estimated_duration_s > s.lane_length_m / 0.2


def test_spacing_wider_than_swath_warns_gaps():
    p = grid(lane_spacing=0.5, swath_width=0.3)
    assert any(i.code == "coverage_gaps" for i in p.issues)
    assert p.summary.nominal_coverage_ratio <= 0.75


@pytest.mark.parametrize("kw", [dict(margin=1.0), dict(margin=5.0)])
def test_margin_too_large(kw):
    with pytest.raises(PlanningError):
        grid(**kw)


def test_invalid_dimensions_rejected_by_schema():
    with pytest.raises(ValueError):
        GridPlanRequest(length=-1, width=2, lane_spacing=0.5, execution_mode="simulation")
    with pytest.raises(ValueError):
        GridPlanRequest(length=1, width=2, lane_spacing=0, execution_mode="simulation")


def test_too_many_lanes():
    with pytest.raises(PlanningError, match="lanes exceed"):
        grid(length=100, width=100, lane_spacing=0.01)


def test_arc_turns_need_margin():
    bad = grid(turn_strategy="arc", turn_radius=0.25, margin=0.0)
    assert not bad.valid and any(i.code == "leaves_boundary" for i in bad.issues)
    good = grid(width=2.1, turn_strategy="arc", turn_radius=0.25, margin=0.3)
    assert good.valid, good.issues
    arcs = [s for s in good.segments if s.kind == SegmentKind.ARC]
    assert arcs and all(abs(abs(a.rotation_deg) - 180) < 1e-6 for a in arcs)


def test_arc_requires_radius():
    with pytest.raises(PlanningError, match="radius"):
        grid(turn_strategy="arc")


def test_mode_mismatch_rejected():
    with pytest.raises(PlanningError, match="never switched automatically"):
        grid(execution_mode="hardware")


def test_concave_polygon_decomposes_into_cells():
    u_shape = [(0, 0), (6, 0), (6, 4), (4, 4), (4, 1), (2, 1), (2, 4), (0, 4)]
    res = plan_coverage(
        CoverageInput(boundary=u_shape, lane_spacing=0.5, swath=0.5, margin=0.0, lane_heading_deg=90, start_point=(0, 0)),
        TIMING,
    )
    assert res.summary.cell_count >= 2
    # lanes never cross the notch
    for s in res.segments:
        if s.kind == SegmentKind.LANE and s.start.y > 1.0:
            assert not (2.0 < (s.start.x + s.end.x) / 2 < 4.0 and min(s.start.x, s.end.x) < 2.0 < max(s.start.x, s.end.x))


def test_self_intersecting_polygon_rejected():
    with pytest.raises(PlanningError, match="invalid"):
        plan_coverage(CoverageInput(boundary=[(0, 0), (2, 2), (2, 0), (0, 2)], lane_spacing=0.5, swath=0.5, margin=0,
                                    lane_heading_deg=0, start_point=(0, 0)), TIMING)


def test_dubins_reaches_goal():
    for q0, q1 in [((0, 0, math.pi / 2), (0.5, 0, -math.pi / 2)), ((0, 0, 0), (3, 2, 1.0)), ((1, 1, 2.0), (-2, 0.5, -2.5))]:
        path = dubins.shortest_path(q0, q1, 0.4)
        q = q0
        for prim, amount in zip(path.word, path.params):
            q = dubins.step(q, prim, amount * (0.4 if prim == "S" else 1.0), 0.4)
        assert math.isclose(q[0], q1[0], abs_tol=1e-6) and math.isclose(q[1], q1[1], abs_tol=1e-6)
        assert math.isclose(math.cos(q[2]), math.cos(q1[2]), abs_tol=1e-6)


def test_enu_roundtrip_and_scale():
    f = EnuFrame(12.97, 77.59, 900)
    e, n = f.to_enu(12.9709, 77.5911, 900)
    lat, lon = f.to_geodetic(e, n)
    assert math.isclose(lat, 12.9709, abs_tol=1e-9) and math.isclose(lon, 77.5911, abs_tol=1e-9)
    assert math.isclose(math.hypot(e, n), haversine_m(12.97, 77.59, 12.9709, 77.5911), rel_tol=2e-3)


def square(lat=12.9716, lon=77.5946, side_m=10.0):
    dlat = side_m / 111_320
    dlon = side_m / (111_320 * math.cos(math.radians(lat)))
    return [dict(lat=lat, lon=lon), dict(lat=lat + dlat, lon=lon), dict(lat=lat + dlat, lon=lon + dlon), dict(lat=lat, lon=lon + dlon)]


def test_gps_plan_in_metres_not_degrees():
    p = plan_gps(CFG, GpsPlanRequest(boundary=square(), lane_spacing=1.0, margin=0.5, execution_mode="simulation"))
    assert p.valid
    assert p.summary.area_m2 == pytest.approx(100.0, rel=0.01)
    assert p.summary.lane_count == 9
    assert all(s.geo_points for s in p.segments)
    wp = p.waypoints[1]
    assert wp.lat is not None and abs(wp.lat - 12.9716) < 0.001


def test_gps_plan_rejects_huge_area():
    with pytest.raises(PlanningError, match="exceeds the local-frame limit"):
        plan_gps(CFG, GpsPlanRequest(boundary=square(side_m=5000), lane_spacing=10, execution_mode="simulation"))


def test_gps_accuracy_warning():
    p = plan_gps(CFG, GpsPlanRequest(boundary=square(), lane_spacing=0.5, execution_mode="simulation"))
    assert any(i.code == "gps_accuracy" for i in p.issues)


def test_auto_lane_heading_follows_long_side():
    lat, lon = 12.9716, 77.5946
    dlat = 20 / 111_320
    dlon = 5 / (111_320 * math.cos(math.radians(lat)))
    rect = [dict(lat=lat, lon=lon), dict(lat=lat + dlat, lon=lon), dict(lat=lat + dlat, lon=lon + dlon), dict(lat=lat, lon=lon + dlon)]
    p = plan_gps(CFG, GpsPlanRequest(boundary=rect, lane_spacing=1.0, execution_mode="simulation"))
    assert min(p.lane_heading_deg % 180, 180 - p.lane_heading_deg % 180) < 1.0  # north-south lanes
