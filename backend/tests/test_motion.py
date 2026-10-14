import time

import pytest

from rover.core.clock import Clock
from rover.core.enums import ControlOwner
from rover.core.events import EventLog, FaultManager
from rover.hal.motors import MotorDriver, WatchdogMotorDriver
from rover.motion.controller import CommandRejected, MotionController
from rover.motion.manual import ManualControlService, direction_to_tank, vector_to_tank
from rover.safety.obstacles import DirectionAssessment, ObstacleAssessment
from rover.core.enums import ObstacleState

from .conftest import make_config


class FakeDriver(MotorDriver):
    driver_name = "fake"
    simulated = True

    def __init__(self):
        super().__init__()
        self.writes = []

    def initialize(self):
        self.initialized = True

    def _apply(self, left, right):
        self.writes.append((left, right))


class ManualClock(Clock):
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


def make_controller(**motion):
    cfg = make_config(motion={"max_accel_per_s": 1000.0, "max_decel_per_s": 1000.0, "reversal_dwell_s": 0.0, **motion})
    events = EventLog()
    drv = FakeDriver()
    drv.initialize()
    clock = ManualClock()
    c = MotionController(drv, cfg, clock, events, FaultManager(events))
    c.permit = lambda: (True, None)
    return c, drv, clock, events


def tick(c, clock, dt=0.02, n=1):
    for _ in range(n):
        clock.t += dt
        c.tick()


def test_lease_expiry_zeroes_output():
    c, drv, clock, events = make_controller()
    c.acquire(ControlOwner.MANUAL, "s1")
    c.command(ControlOwner.MANUAL, "s1", 0.5, 0.5, ttl_s=0.1)
    tick(c, clock)
    assert c.applied == [0.5, 0.5]
    time.sleep(0.15)
    tick(c, clock)
    assert c.applied == [0.0, 0.0]
    assert drv.outputs == (0.0, 0.0)
    assert any(e.code == "motion.lease_expired" for e in events.recent())


def test_only_owner_may_command_and_acquire_conflicts():
    c, *_ = make_controller()
    c.acquire(ControlOwner.MISSION, "m1")
    with pytest.raises(CommandRejected) as exc:
        c.acquire(ControlOwner.MANUAL, "s1")
    assert exc.value.code == "controller_busy"
    with pytest.raises(CommandRejected) as exc:
        c.command(ControlOwner.MANUAL, "s1", 0.2, 0.2, 0.3)
    assert exc.value.code == "not_owner"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, -1.01, True, "0.5"])
def test_invalid_values_rejected(bad):
    c, *_ = make_controller()
    c.acquire(ControlOwner.MANUAL, "s1")
    with pytest.raises(CommandRejected):
        c.command(ControlOwner.MANUAL, "s1", bad, 0.0, 0.3)


def test_not_permitted_rejects_motion_but_allows_zero():
    c, *_ = make_controller()
    c.permit = lambda: (False, "rover is disarmed")
    c.acquire(ControlOwner.MANUAL, "s1")
    with pytest.raises(CommandRejected) as exc:
        c.command(ControlOwner.MANUAL, "s1", 0.3, 0.3, 0.3)
    assert exc.value.code == "motion_not_permitted"
    c.command(ControlOwner.MANUAL, "s1", 0.0, 0.0, 0.3)


def test_max_output_and_accel_limits():
    c, drv, clock, _ = make_controller(max_output=0.6, max_accel_per_s=1.0)
    c.acquire(ControlOwner.MANUAL, "s")
    c.command(ControlOwner.MANUAL, "s", 1.0, 1.0, 1.0)
    tick(c, clock, dt=0.1)
    assert c.applied[0] == pytest.approx(0.1)
    tick(c, clock, dt=0.1, n=10)
    assert c.applied[0] == pytest.approx(0.6)


def test_reversal_interlock_ramps_through_zero_and_dwells():
    c, drv, clock, _ = make_controller(max_decel_per_s=5.0, max_accel_per_s=5.0, reversal_dwell_s=0.2)
    c.acquire(ControlOwner.MANUAL, "s")
    c.command(ControlOwner.MANUAL, "s", 0.5, 0.5, 2.0)
    tick(c, clock, dt=0.05, n=4)
    assert c.applied[0] == pytest.approx(0.5)
    c.command(ControlOwner.MANUAL, "s", -0.5, -0.5, 2.0)
    seen = []
    for _ in range(20):
        tick(c, clock, dt=0.05)
        seen.append(c.applied[0])
    first_neg = next(i for i, v in enumerate(seen) if v < 0)
    zeros = [v for v in seen[:first_neg] if v == 0.0]
    assert len(zeros) >= 3  # held at zero for the dwell (0.2 s at 0.05 s ticks)
    assert all(v >= 0 for v in seen[:first_neg])


def test_emergency_halt_is_immediate_and_invalidates_lease():
    c, drv, clock, _ = make_controller(max_decel_per_s=0.1)
    c.acquire(ControlOwner.MANUAL, "s")
    c.command(ControlOwner.MANUAL, "s", 0.8, 0.8, 2.0)
    tick(c, clock, n=3)
    c.emergency_halt("test")
    assert c.applied == [0.0, 0.0] and drv.outputs == (0.0, 0.0)
    tick(c, clock, n=3)
    assert c.applied == [0.0, 0.0]  # stale lease from the old epoch is ignored


def _assess(front_blocked=False, rear_blocked=False):
    return ObstacleAssessment(
        DirectionAssessment(ObstacleState.OBSTACLE if front_blocked else ObstacleState.CLEAR, blocked=front_blocked, reasons=["x"]),
        DirectionAssessment(ObstacleState.OBSTACLE if rear_blocked else ObstacleState.CLEAR, blocked=rear_blocked),
        [],
    )


def test_obstacle_blocks_forward_only():
    c, drv, clock, _ = make_controller()
    c.obstacles = lambda: _assess(front_blocked=True)
    c.acquire(ControlOwner.MANUAL, "s")
    c.command(ControlOwner.MANUAL, "s", 0.5, 0.5, 1.0)
    tick(c, clock)
    assert c.applied == [0.0, 0.0] and c.blocked_reason
    c.command(ControlOwner.MANUAL, "s", -0.5, -0.5, 1.0)
    tick(c, clock)
    assert c.applied == [-0.5, -0.5]
    c.command(ControlOwner.MANUAL, "s", 0.5, -0.5, 1.0)  # pivot allowed
    tick(c, clock, n=30)
    assert c.applied == [0.5, -0.5]


def test_driver_watchdog_trips_when_updates_stop():
    inner = FakeDriver()
    tripped = []
    wd = WatchdogMotorDriver(inner, 0.1, on_trip=lambda: tripped.append(1))
    wd.initialize()
    wd.set_outputs(0.7, 0.7)
    time.sleep(0.3)
    assert inner.writes[-1] == (0.0, 0.0)
    assert wd.tripped and tripped
    wd.close()


def test_direction_mapping():
    assert direction_to_tank("forward", 0.5, 0.5) == (0.5, 0.5)
    assert direction_to_tank("right", 0.5, 0.5) == (0.5, -0.5)
    l, r = direction_to_tank("forward_left", 1.0, 0.4)
    assert l < r
    l, r = direction_to_tank("reverse_left", 1.0, 0.4)
    assert l < 0 and r < 0 and abs(l) < abs(r)
    with pytest.raises(CommandRejected):
        direction_to_tank("sideways", 1.0, 0.5)
    assert vector_to_tank(1.0, 1.0) == (1.0, 0.0)


# ---------------------------------------------------------------- manual service


def make_manual(**manual):
    c, drv, clock, events = make_controller()
    cfg = make_config(manual={"deadman_timeout_s": 0.15, **manual})
    svc = ManualControlService(cfg, c, events)
    return svc, c, clock


def test_deadman_stops_when_heartbeats_stop():
    svc, c, clock = make_manual()
    s = svc.open_session("tablet")
    svc.drive(s.session_id, 1, {"direction": "forward", "speed": 0.5})
    tick(c, clock)
    assert c.applied == [0.5, 0.5]
    time.sleep(0.25)
    tick(c, clock)
    assert c.applied == [0.0, 0.0]
    assert svc.session.deadman_trips == 1


def test_stale_sequence_rejected():
    svc, c, clock = make_manual()
    s = svc.open_session("tablet")
    svc.drive(s.session_id, 5, {"direction": "forward", "speed": 0.5})
    with pytest.raises(CommandRejected) as exc:
        svc.drive(s.session_id, 5, {"direction": "forward", "speed": 0.5})
    assert exc.value.code == "stale_command"


def test_old_session_cannot_replay_after_reconnect():
    svc, c, clock = make_manual()
    old = svc.open_session("tablet")
    svc.close_session(old.session_id)
    svc.open_session("tablet")
    with pytest.raises(CommandRejected) as exc:
        svc.drive(old.session_id, 100, {"direction": "forward", "speed": 0.5})
    assert exc.value.code == "stale_session"


def test_second_client_needs_takeover():
    svc, c, clock = make_manual()
    svc.open_session("a")
    with pytest.raises(CommandRejected):
        svc.open_session("b")
    s = svc.open_session("b", takeover=True)
    assert svc.session.session_id == s.session_id


def test_manual_rejected_while_mission_owns_controller():
    svc, c, clock = make_manual()
    c.acquire(ControlOwner.MISSION, "m")
    with pytest.raises(CommandRejected) as exc:
        svc.open_session("a")
    assert exc.value.code == "mission_active"


def test_hold_limit():
    svc, c, clock = make_manual(max_continuous_s=1.0)
    s = svc.open_session("a")
    svc.drive(s.session_id, 1, {"direction": "forward", "speed": 0.4})
    svc.session.motion_started_at -= 2.0
    with pytest.raises(CommandRejected) as exc:
        svc.drive(s.session_id, 2, {"direction": "forward", "speed": 0.4})
    assert exc.value.code == "hold_limit"
    with pytest.raises(CommandRejected):
        svc.drive(s.session_id, 3, {"direction": "forward", "speed": 0.4})
    svc.drive(s.session_id, 4, {"direction": "stop"})
    assert svc.drive(s.session_id, 5, {"direction": "forward", "speed": 0.4})["accepted"]


def test_manual_speed_capped_and_invalid_rejected():
    svc, c, clock = make_manual(max_speed=0.6)
    s = svc.open_session("a")
    out = svc.drive(s.session_id, 1, {"direction": "forward", "speed": 1.0})
    assert out["left"] == pytest.approx(0.6)
    with pytest.raises(CommandRejected):
        svc.drive(s.session_id, 2, {"mode": "vector", "throttle": 2.0, "steer": 0})
