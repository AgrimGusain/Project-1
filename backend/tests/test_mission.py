import pytest

from rover.core.enums import MissionState as S
from rover.core.enums import MissionStatus
from rover.mission.manager import MissionError
from rover.mission.state_machine import TRANSITIONS, InvalidTransition, MissionStateMachine

from .conftest import make_config, wait_until

GRID = {"length": 1.2, "width": 0.6, "lane_spacing": 0.3, "margin": 0.0, "speed_mps": 0.3, "execution_mode": "simulation"}


def test_state_machine_rejects_invalid_transitions():
    sm = MissionStateMachine()
    with pytest.raises(InvalidTransition):
        sm.transition(S.RUNNING)
    sm.transition(S.READY)
    sm.transition(S.ARMING)
    sm.transition(S.RUNNING)
    with pytest.raises(InvalidTransition):
        sm.transition(S.READY)
    sm.transition(S.EMERGENCY_STOP)
    assert TRANSITIONS[S.EMERGENCY_STOP] == {S.IDLE}  # never straight back to RUNNING
    with pytest.raises(InvalidTransition):
        sm.transition(S.RUNNING)


def test_every_state_can_reach_emergency_stop():
    for state, targets in TRANSITIONS.items():
        if state != S.EMERGENCY_STOP:
            assert S.EMERGENCY_STOP in targets


def fast_config(**kw):
    sim = {"speed_factor": 15.0, "obstacles": [], **kw.pop("simulation", {})}
    return make_config(simulation=sim, **kw)


async def _loaded(rt, params=None):
    rec = rt.missions.create("t", "grid", params or GRID)
    rt.missions.load(rec["id"])
    return rec


async def test_start_refused_when_disarmed(runtime_factory):
    rt = await runtime_factory(fast_config())
    await _loaded(rt)
    with pytest.raises(MissionError) as exc:
        await rt.missions.start()
    assert "disarmed" in str(exc.value.details)
    assert rt.missions.state == S.READY


async def test_open_loop_mission_completes(runtime_factory):
    rt = await runtime_factory(fast_config())
    rec = await _loaded(rt)
    assert rt.missions.active.execution == "open_loop"
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.COMPLETED, timeout=20), rt.missions.snapshot()
    m = rt.repo.get_mission(rec["id"])
    assert m["status"] == MissionStatus.COMPLETED.value
    assert m["progress"]["percent"] == pytest.approx(100.0)
    assert m["progress"]["estimated_pose_kind"] == "commanded_dead_reckoning"
    assert m["progress"]["measured_pose"] is None  # no fabricated measurement
    transitions = [e["to_state"] for e in rt.repo.mission_events(rec["id"]) if e["type"] == "state_transition"]
    assert transitions[:4] == ["READY", "ARMING", "RUNNING", "COMPLETED"]
    assert rt.controller.owner.value == "none"


async def test_closed_loop_mission_follows_path(runtime_factory):
    rt = await runtime_factory(fast_config(localization={"provider": "simulated"}))
    await _loaded(rt, {**GRID, "length": 2.0, "width": 1.0})
    assert rt.missions.active.execution == "closed_loop"
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    max_xte = 0.0

    def done():
        nonlocal max_xte
        p = (rt.missions.snapshot()["mission"] or {}).get("progress") or {}
        if p.get("cross_track_m") is not None and p.get("phase") == "lane":
            max_xte = max(max_xte, abs(p["cross_track_m"]))
        return rt.missions.state == S.COMPLETED

    assert await wait_until(done, timeout=30), rt.missions.snapshot()
    end = rt.world.truth()["pose"]
    last = rt.missions.active.plan.segments[-1].end
    assert abs(end["x"] - last.x) < 0.2 and abs(end["y"] - last.y) < 0.2
    assert max_xte < 0.15


async def test_pause_resume_and_stop(runtime_factory):
    rt = await runtime_factory(fast_config(simulation={"speed_factor": 2.0, "obstacles": []}))
    rec = await _loaded(rt, {**GRID, "length": 5.0})
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.RUNNING)
    rt.missions.pause()
    assert rt.missions.state == S.PAUSED
    await wait_until(lambda: rt.controller.applied == [0.0, 0.0], timeout=2)
    assert rt.controller.applied == [0.0, 0.0]
    rt.missions.resume()
    assert rt.missions.state == S.RUNNING
    await rt.missions.stop()
    assert rt.missions.state == S.IDLE
    assert rt.repo.get_mission(rec["id"])["status"] == "cancelled"
    assert rt.controller.owner.value == "none"


async def test_emergency_stop_invalidates_and_never_resumes(runtime_factory):
    rt = await runtime_factory(fast_config(simulation={"speed_factor": 2.0, "obstacles": []}))
    rec = await _loaded(rt, {**GRID, "length": 5.0})
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.controller.applied != [0.0, 0.0], timeout=5)
    rt.supervisor.emergency_stop("test", "unit test")
    assert rt.controller.applied == [0.0, 0.0]
    assert rt.missions.state == S.EMERGENCY_STOP
    assert not rt.supervisor.armed
    with pytest.raises(MissionError):
        rt.missions.resume()
    rt.supervisor.reset_emergency_stop()
    assert rt.missions.state == S.IDLE and not rt.supervisor.armed
    assert rt.repo.get_mission(rec["id"])["status"] == "emergency_stopped"
    with pytest.raises(MissionError):
        rt.missions.load(rec["id"])  # an e-stopped mission cannot be reloaded


async def test_obstacle_pauses_mission(runtime_factory):
    cfg = make_config(simulation={"speed_factor": 5.0, "obstacles": [{"type": "circle", "x": 0.15, "y": 1.6, "r": 0.15}]})
    rt = await runtime_factory(cfg)
    await _loaded(rt, {**GRID, "length": 3.0})
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.PAUSED, timeout=20)
    assert rt.missions.active.pause_reason.startswith("obstacle")
    assert not rt.world.truth()["collided"]
    with pytest.raises(MissionError) as exc:
        rt.missions.resume()
    assert exc.value.code == "resume_refused"


async def test_manual_and_mission_interlock(runtime_factory):
    rt = await runtime_factory(fast_config(simulation={"speed_factor": 1.0, "obstacles": []}))
    await _loaded(rt, {**GRID, "length": 5.0})
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    s = rt.manual.open_session("tablet")
    with pytest.raises(MissionError) as exc:
        await rt.missions.start()
    assert "manual" in str(exc.value.details)
    rt.manual.close_session(s.session_id)
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.RUNNING)
    from rover.motion.controller import CommandRejected

    with pytest.raises(CommandRejected):
        rt.manual.open_session("tablet")


async def test_disarm_pauses_running_mission(runtime_factory):
    rt = await runtime_factory(fast_config(simulation={"speed_factor": 1.0, "obstacles": []}))
    await _loaded(rt, {**GRID, "length": 5.0})
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.RUNNING)
    rt.supervisor.disarm()
    assert rt.missions.state == S.PAUSED
    with pytest.raises(MissionError):
        rt.missions.resume()


async def test_interrupted_mission_recovered_after_restart(tmp_path):
    from rover.runtime import RoverRuntime

    db = str(tmp_path / "r.db")
    rt = RoverRuntime(fast_config(), db)
    await rt.start()
    rec = rt.missions.create("crash", "grid", GRID)
    rt.repo.update_mission(rec["id"], status="active")
    # simulate an unclean exit: skip rt.stop() bookkeeping for the mission
    rt.controller.emergency_halt("test")
    await rt.stop()
    rt2 = RoverRuntime(fast_config(), db)
    await rt2.start()
    try:
        m = rt2.repo.get_mission(rec["id"])
        assert m["status"] == "interrupted" and m["review_required"]
        assert rt2.missions.state == S.IDLE
        assert not rt2.supervisor.armed
        with pytest.raises(MissionError):
            rt2.missions.load(rec["id"])
        rt2.missions.acknowledge_review(rec["id"], "checked field")
        assert not rt2.repo.get_mission(rec["id"])["review_required"]
    finally:
        await rt2.stop()


async def test_gps_mission_requires_gnss(runtime_factory):
    rt = await runtime_factory(fast_config())
    from .test_planner import square

    rec = rt.missions.create("gps", "gps", {"boundary": square(side_m=4), "lane_spacing": 1.0, "execution_mode": "simulation"})
    rt.missions.load(rec["id"])
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    with pytest.raises(MissionError) as exc:
        await rt.missions.start()
    assert "GNSS" in str(exc.value.details)


async def test_gps_mission_runs_with_simulated_gps(runtime_factory):
    cfg = fast_config(localization={"provider": "simulated_gps", "gps": {"max_accuracy_m": 0.5}},
                      simulation={"speed_factor": 5.0})
    rt = await runtime_factory(cfg)
    from .test_planner import square

    sim_o = cfg.simulation.geo_origin
    rec = rt.missions.create("gps", "gps", {"boundary": square(sim_o.lat, sim_o.lon, side_m=3), "lane_spacing": 1.0,
                                            "margin": 0.2, "execution_mode": "simulation"})
    rt.missions.load(rec["id"])
    assert rt.missions.active.execution == "gnss"
    await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.COMPLETED, timeout=40), rt.missions.snapshot()
    assert rt.localization.estimate().simulated is True


async def test_gps_loss_pauses_mission(runtime_factory):
    cfg = make_config(simulation={"speed_factor": 1.0}, localization={"provider": "simulated_gps", "gps": {"max_accuracy_m": 0.5}})
    rt = await runtime_factory(cfg)
    from .test_planner import square

    o = cfg.simulation.geo_origin
    rec = rt.missions.create("gps", "gps", {"boundary": square(o.lat, o.lon, side_m=6), "lane_spacing": 1.0, "execution_mode": "simulation"})
    rt.missions.load(rec["id"])
    await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: rt.missions.state == S.RUNNING)
    rt.world.inject_fault("gps_loss")
    assert await wait_until(lambda: rt.missions.state == S.PAUSED, timeout=5)
    assert rt.missions.active.pause_reason == "localization lost"
    est = rt.localization.estimate()
    assert not est.position_valid and est.lat is None
    with pytest.raises(MissionError):
        rt.missions.resume()
    rt.world.clear_fault("gps_loss")
    assert await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    rt.missions.resume()
    assert rt.missions.state == S.RUNNING


async def test_gps_mission_approaches_start_and_enforces_distance(runtime_factory):
    from .test_planner import square

    cfg = fast_config(
        localization={"provider": "simulated_gps", "gps": {"max_accuracy_m": 0.5}, "max_approach_distance_m": 10},
        simulation={"speed_factor": 5.0, "place_rover_at_start": False, "start_pose": {"x": -3.0, "y": -2.0, "heading_deg": 90}},
    )
    rt = await runtime_factory(cfg)
    o = cfg.simulation.geo_origin
    rec = rt.missions.create("approach", "gps", {"boundary": square(o.lat, o.lon, side_m=3), "lane_spacing": 1.0,
                                                 "margin": 0.2, "execution_mode": "simulation"})
    rt.missions.load(rec["id"])
    await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    await rt.missions.start()
    assert await wait_until(lambda: (rt.missions.snapshot()["mission"]["progress"] or {}).get("phase") == "approach", timeout=5)
    assert await wait_until(lambda: rt.missions.state == S.COMPLETED, timeout=60), rt.missions.snapshot()


async def test_gps_mission_refused_when_too_far(runtime_factory):
    from .test_planner import square

    cfg = fast_config(
        localization={"provider": "simulated_gps", "gps": {"max_accuracy_m": 0.5}, "max_approach_distance_m": 5},
        simulation={"place_rover_at_start": False, "start_pose": {"x": -30.0, "y": 0.0, "heading_deg": 0}},
    )
    rt = await runtime_factory(cfg)
    o = cfg.simulation.geo_origin
    rec = rt.missions.create("far", "gps", {"boundary": square(o.lat, o.lon, side_m=3), "lane_spacing": 1.0, "execution_mode": "simulation"})
    rt.missions.load(rec["id"])
    await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    with pytest.raises(MissionError) as exc:
        await rt.missions.start()
    assert "from the mission start" in str(exc.value.details)
