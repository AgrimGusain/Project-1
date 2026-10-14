"""Sensor framework and optional-hardware behaviour."""

import math

import pytest

from rover.config import MagnetometerSensorConfig
from rover.localization.magnetic import assess_heading
from rover.sensors.base import ConnectionState, ReadingHealth, Sensor, SensorError
from rover.sensors.drivers import DriverContext, create_sensor
from rover.sim.world import SIM_HARD_IRON_UT, SimWorld

from .conftest import make_config, wait_until


async def test_simulated_sensors_report_simulated(runtime):
    snap = {s["id"]: s for s in runtime.sensors.snapshot()}
    assert snap["front_ultrasonic"]["simulated"] is True
    assert snap["front_ultrasonic"]["physically_verified"] is False
    assert await wait_until(lambda: runtime.sensors.get("front_ultrasonic").reading_health() == ReadingHealth.OK)
    r = runtime.sensors.get("front_ultrasonic").last_reading
    assert r.simulated and r.units["distance"] == "m"


async def test_invalid_measurement_not_treated_as_valid(runtime):
    ch = runtime.sensors.get("front_ultrasonic")
    await wait_until(lambda: ch.reading_health() == ReadingHealth.OK)
    runtime.world.inject_fault("ultrasonic_no_echo")
    assert await wait_until(lambda: ch.reading_health() == ReadingHealth.INVALID)
    assert ch.usable_reading() is None
    assert await wait_until(lambda: runtime.supervisor.assessment.front.blocked)
    assert runtime.supervisor.assessment.front.state.value == "invalid"


async def test_stale_measurement_detected(runtime):
    ch = runtime.sensors.get("front_ultrasonic")
    await wait_until(lambda: ch.reading_health() == ReadingHealth.OK)
    runtime.world.inject_fault("ultrasonic_stale")
    assert await wait_until(lambda: ch.reading_health() == ReadingHealth.STALE, timeout=3)
    assert ch.state == ConnectionState.CONNECTED  # device still connected, data stale
    assert await wait_until(lambda: runtime.supervisor.assessment.front.state.value == "stale")


async def test_disconnect_and_reconnect(runtime_factory, config_factory):
    cfg = config_factory(sensors=[
        {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated", "max_consecutive_errors": 2,
         "reconnect_interval_s": 0.2},
        {"id": "magnetometer", "type": "magnetometer", "driver": "simulated", "max_consecutive_errors": 2,
         "reconnect_interval_s": 0.2},
    ])
    rt = await runtime_factory(cfg)
    mag = rt.sensors.get("magnetometer")
    assert await wait_until(lambda: mag.state == ConnectionState.CONNECTED)
    rt.world.inject_fault("magnetometer_disconnect")
    assert await wait_until(lambda: mag.state in (ConnectionState.DISCONNECTED, ConnectionState.FAULTY), timeout=3)
    assert rt.faults.is_active("sensor_magnetometer_unavailable")
    # optional sensor: warning only, does not block arming
    assert not any(f.code == "sensor_magnetometer_unavailable" for f in rt.faults.blocking_arming())
    rt.world.clear_fault("magnetometer_disconnect")
    assert await wait_until(lambda: mag.state == ConnectionState.CONNECTED, timeout=3)
    assert mag.reconnects >= 1
    assert not rt.faults.is_active("sensor_magnetometer_unavailable")


async def test_required_sensor_failure_blocks_arming_only(runtime_factory, config_factory):
    cfg = config_factory(sensors=[
        {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated", "max_consecutive_errors": 1,
         "reconnect_interval_s": 5},
    ])
    rt = await runtime_factory(cfg)
    rt.world.inject_fault("ultrasonic_disconnect")
    assert await wait_until(lambda: rt.faults.is_active("sensor_front_ultrasonic_unavailable"), timeout=3)
    assert rt.faults.blocking_arming()
    from rover.safety.supervisor import ArmingRefused

    with pytest.raises(ArmingRefused):
        rt.supervisor.arm()
    # planning still works
    from rover.planning.models import GridPlanRequest
    from rover.planning.service import plan_grid

    assert plan_grid(rt.config, GridPlanRequest(length=2, width=1, lane_spacing=0.5, execution_mode="simulation")).valid


async def test_magnetometer_three_axis_reading(runtime_factory, config_factory):
    cfg = config_factory(sensors=[
        {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated"},
        {"id": "magnetometer", "type": "magnetometer", "driver": "simulated"},
    ])
    rt = await runtime_factory(cfg)
    ch = rt.sensors.get("magnetometer")
    assert await wait_until(lambda: ch.usable_reading() is not None)
    r = ch.usable_reading()
    assert set(r.value) >= {"x", "y", "z", "magnitude"}
    assert all(r.units[k] == "uT" for k in ("x", "y", "z"))
    assert r.metadata["frame"].startswith("body")
    assert ch.snapshot()["calibration"]["state"] == "uncalibrated"


def _mag_reading(world):
    from rover.sensors.drivers.simulated import SimMagnetometer

    cfg = MagnetometerSensorConfig(id="mag")
    return SimMagnetometer(cfg, DriverContext(make_config(), world=world)).acquire()


def test_uncalibrated_magnetometer_heading_is_unavailable():
    world = SimWorld(make_config(), clock=None)  # type: ignore[arg-type]
    world.reset(heading_deg=40)
    res = assess_heading(_mag_reading(world), MagnetometerSensorConfig(id="mag"), motors_active=False)
    assert res["available"] and not res["reliable"] and res["heading_deg"] is None
    assert any("not calibrated" in r for r in res["reasons"])
    assert any("tilt" in r for r in res["reasons"])


def test_calibrated_level_magnetometer_heading():
    world = SimWorld(make_config(), clock=None)  # type: ignore[arg-type]
    world.reset(heading_deg=40)
    cfg = MagnetometerSensorConfig(
        id="mag", assume_level=True,
        calibration={"calibrated": True, "hard_iron_ut": SIM_HARD_IRON_UT, "motor_interference_checked": False},
    )
    res = assess_heading(_mag_reading(world), cfg, motors_active=False)
    assert res["reliable"], res["reasons"]
    assert math.isclose(res["heading_deg"], 40, abs_tol=2.0)
    # motors running with uncharacterised interference -> unreliable
    res = assess_heading(_mag_reading(world), cfg, motors_active=True)
    assert not res["reliable"]


async def test_all_optional_sensors_disabled_app_starts(runtime_factory):
    from rover.config import load_config
    import pathlib

    cfg = load_config(pathlib.Path(__file__).resolve().parents[1] / "config" / "minimal_simulation.yaml")
    rt = await runtime_factory(cfg)
    assert await wait_until(lambda: rt.controller.loop_healthy)
    snap = rt.sensors.snapshot()
    assert all(s["state"] == "disabled" and s["last_reading"] is None for s in snap)
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["obstacle_sensing"]["status"] == "not_installed"
    assert caps["magnetometer"]["status"] == "not_installed"
    assert caps["gps_navigation"]["status"] == "not_installed"
    assert caps["grid_planning"]["status"] == "ready"
    assert caps["manual_control"]["status"] == "simulated"
    rt.supervisor.arm()  # nothing required -> arming possible


class ToyHumiditySensor(Sensor):
    """An 'external' adapter defined outside the package: plugs in without code changes elsewhere."""

    sensor_type = "temperature"
    driver_name = "toy_external"
    simulated = False

    def acquire(self):
        return self.reading({"temperature": 21.5}, {"temperature": "degC"})


class FlakySensor(Sensor):
    sensor_type = "battery"
    driver_name = "flaky"
    calls = 0

    def acquire(self):
        FlakySensor.calls += 1
        raise SensorError("bus error")


async def test_external_adapter_plugs_in(runtime_factory, config_factory):
    cfg = config_factory(sensors=[
        {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated"},
        {"id": "ext_temp", "type": "temperature", "driver": "external", "adapter": "tests.test_sensors:ToyHumiditySensor"},
        {"id": "bad_batt", "type": "battery", "driver": "external", "adapter": "tests.test_sensors:FlakySensor",
         "max_consecutive_errors": 2, "reconnect_interval_s": 0.2},
        {"id": "missing", "type": "imu", "driver": "external", "adapter": "nonexistent.module:Nope"},
    ])
    rt = await runtime_factory(cfg)
    ext = rt.sensors.get("ext_temp")
    assert await wait_until(lambda: ext.usable_reading() is not None)
    assert ext.usable_reading().simulated is False
    assert await wait_until(lambda: rt.sensors.get("bad_batt").state == ConnectionState.DISCONNECTED)
    missing = rt.sensors.get("missing")
    assert await wait_until(lambda: missing.state == ConnectionState.FAULTY)
    assert "cannot load adapter" in missing.error
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["temperature_monitoring"]["status"] in ("ready", "degraded")
    assert caps["battery_monitoring"]["status"] == "unavailable"
    assert caps["imu"]["status"] == "unavailable"


def test_simulated_driver_refused_in_hardware_context():
    from rover.sensors.base import SensorInitError

    cfg = make_config()
    hw = cfg.model_copy(update={"mode": "hardware"})
    with pytest.raises(SensorInitError):
        create_sensor(cfg.sensors[0], DriverContext(hw, world=None))


async def test_missing_metal_detector_produces_no_events(runtime):
    assert runtime.repo.list_detections() == []
    assert not runtime.sensors.of_type("metal_detector")


async def test_simulated_metal_events_flagged_simulated(runtime_factory, config_factory):
    cfg = config_factory(
        sensors=[{"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated"},
                 {"id": "metal", "type": "metal_detector", "driver": "simulated", "rate_hz": 20}],
        simulation={"metal_targets": [{"x": 0.0, "y": 0.18, "strength": 1.0}], "speed_bias": 0.0},
    )
    rt = await runtime_factory(cfg)
    assert await wait_until(lambda: bool(rt.repo.list_detections()), timeout=3)
    det = rt.repo.list_detections()[0]
    assert det["simulated"] is True
    assert det["position"] is None  # no localization -> no invented position
    assert det["localization_quality"].startswith("none")
    assert det["review_status"] == "unreviewed"


async def test_manual_driving_without_any_optional_hardware(runtime_factory):
    from rover.config import load_config
    import pathlib

    cfg = load_config(pathlib.Path(__file__).resolve().parents[1] / "config" / "minimal_simulation.yaml")
    rt = await runtime_factory(cfg)
    assert await wait_until(lambda: rt.controller.loop_healthy)
    rt.supervisor.arm()
    s = rt.manual.open_session("tablet")
    rt.manual.drive(s.session_id, 1, {"direction": "forward", "speed": 0.5})
    assert await wait_until(lambda: rt.controller.applied[0] > 0.2, timeout=2)
    rt.manual.stop(s.session_id)
    assert await wait_until(lambda: rt.controller.applied == [0.0, 0.0], timeout=2)


async def test_hardware_mode_failed_init_never_falls_back_to_simulation(runtime_factory):
    """On a machine without lgpio/GPIO, a hardware config must come up with motors and
    GPIO sensors *unavailable* — never silently simulated — while the app stays usable."""
    import importlib.util

    if importlib.util.find_spec("lgpio") is not None:
        pytest.skip("lgpio present: this test needs a host without GPIO")
    from rover.config import RoverConfig

    cfg = RoverConfig.model_validate({
        "mode": "hardware",
        "server": {"frontend_dist": None},
        "motors": {"driver": "l298n"},
        "camera": {"provider": "none"},
        "safety": {"auto_disarm_idle_s": 0},
        "sensors": [
            {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "hcsr04", "trigger_pin": 24, "echo_pin": 25,
             "reconnect_interval_s": 60},
            {"id": "soc_temperature", "type": "temperature", "driver": "linux_thermal", "reconnect_interval_s": 60},
        ],
    })
    rt = await runtime_factory(cfg)
    assert rt.world is None
    assert "motors" in rt.hardware_errors
    drv = rt.controller.driver.status()
    assert drv["simulated"] is False and drv["healthy"] is False
    us = rt.sensors.get("front_ultrasonic")
    assert await wait_until(lambda: us.state == ConnectionState.FAULTY, timeout=3)
    assert us.simulated is False and us.last_reading is None
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["manual_control"]["status"] == "unavailable"
    assert all(c["status"] != "simulated" for c in caps.values())
    assert caps["grid_planning"]["status"] == "ready"  # unrelated features still work
    from rover.safety.supervisor import ArmingRefused

    with pytest.raises(ArmingRefused):
        rt.supervisor.arm()
    from rover.planning.models import GridPlanRequest
    from rover.planning.service import plan_grid

    assert plan_grid(cfg, GridPlanRequest(length=2, width=1, lane_spacing=0.5, execution_mode="hardware")).valid
