import pytest
from pydantic import ValidationError

from rover.config import RoverConfig, load_config

from .conftest import make_config


def test_default_config_is_simulation_and_valid():
    cfg = RoverConfig()
    assert cfg.is_simulation
    assert {s.id for s in cfg.sensors} == {"front_ultrasonic", "front_ir"}


def test_hardware_mode_rejects_simulated_devices():
    with pytest.raises(ValidationError) as exc:
        RoverConfig.model_validate({"mode": "hardware"})
    msg = str(exc.value)
    assert "real motor driver" in msg
    assert "cannot use a simulated sensor" in msg


def test_simulation_mode_rejects_gpio_drivers():
    with pytest.raises(ValidationError, match="cannot use GPIO driver"):
        make_config(sensors=[{"id": "us1", "type": "ultrasonic", "driver": "hcsr04", "trigger_pin": 1, "echo_pin": 2}],
                    safety={"required_for_arming": []})


def test_hardware_gps_requires_gps_provider():
    data = {
        "mode": "hardware_gps",
        "motors": {"driver": "l298n"},
        "sensors": [{"id": "front_ultrasonic", "type": "ultrasonic", "driver": "hcsr04", "trigger_pin": 24, "echo_pin": 25}],
    }
    with pytest.raises(ValidationError, match="requires localization.provider=gps_nmea"):
        RoverConfig.model_validate(data)
    data["localization"] = {"provider": "gps_nmea"}
    assert RoverConfig.model_validate(data).mode.value == "hardware_gps"


def test_duplicate_gpio_pins_rejected():
    data = {
        "mode": "hardware",
        "motors": {"driver": "l298n"},
        "sensors": [{"id": "front_ultrasonic", "type": "ultrasonic", "driver": "hcsr04", "trigger_pin": 17, "echo_pin": 25}],
    }
    with pytest.raises(ValidationError, match="GPIO pins assigned more than once"):
        RoverConfig.model_validate(data)


def test_required_sensor_must_exist_and_be_enabled():
    with pytest.raises(ValidationError, match="unknown sensor"):
        make_config(safety={"required_for_arming": ["nope"]})
    with pytest.raises(ValidationError, match="disabled"):
        make_config(sensors=[{"id": "front_ultrasonic", "type": "ultrasonic", "enabled": False}])


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        RoverConfig.model_validate({"safety": {"obstacle_stop_distanse_m": 0.3}})


def test_external_driver_requires_adapter():
    with pytest.raises(ValidationError, match="requires 'adapter"):
        make_config(sensors=[{"id": "mag", "type": "magnetometer", "driver": "external"}], safety={"required_for_arming": []})


def test_duplicate_sensor_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate sensor ids"):
        make_config(sensors=[{"id": "a1", "type": "imu"}, {"id": "a1", "type": "battery"}], safety={"required_for_arming": []})


def test_shipped_profiles_validate():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "config"
    for f in root.glob("*.yaml"):
        load_config(f)


def test_auth_enabled_without_token_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("ROVER_AUTH_TOKEN", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("auth: {enabled: true}\n")
    with pytest.raises(ValueError, match="no token"):
        load_config(p)
    monkeypatch.setenv("ROVER_AUTH_TOKEN", "s3cret")
    assert load_config(p).auth.token == "s3cret"
