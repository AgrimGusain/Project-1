import time

import pytest
from fastapi.testclient import TestClient

from rover.api.app import create_app

from .conftest import make_config


@pytest.fixture
def client(tmp_path):
    app = create_app(make_config(manual={"deadman_timeout_s": 0.2}), str(tmp_path / "api.db"))
    with TestClient(app) as c:
        # wait for the control loop
        for _ in range(100):
            if c.get("/api/health").json()["control_loop_ok"]:
                break
            time.sleep(0.02)
        yield c


def test_health_and_status(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["mode"] == "simulation" and h["simulated"]
    st = client.get("/api/status").json()
    assert st["safety"]["armed"] is False  # always starts disarmed
    assert st["mission"]["state"] == "IDLE"
    assert {c["id"] for c in st["capabilities"]} >= {"manual_control", "gps_navigation", "magnetometer"}


def test_plan_preview_and_validation_errors(client):
    r = client.post("/api/plan/grid", json={"length": 3, "width": 2, "lane_spacing": 0.5, "execution_mode": "simulation"})
    assert r.status_code == 200 and r.json()["valid"]
    r = client.post("/api/plan/grid", json={"length": -3, "width": 2, "lane_spacing": 0.5, "execution_mode": "simulation"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.post("/api/plan/grid", json={"length": 3, "width": 2, "lane_spacing": 0.5, "margin": 1.5, "execution_mode": "simulation"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "planning_failed"
    r = client.post("/api/plan/grid", json={"length": 3, "width": 2, "lane_spacing": 0.5, "execution_mode": "hardware"})
    assert r.status_code == 422


def test_mission_crud_and_invalid_transitions(client):
    r = client.post("/api/missions", json={"name": "m1", "kind": "grid",
                                           "params": {"length": 2, "width": 1, "lane_spacing": 0.5, "execution_mode": "simulation"}})
    assert r.status_code == 201
    mid = r.json()["id"]
    assert client.get(f"/api/missions/{mid}").json()["plan"]["valid"]
    assert client.post("/api/mission/resume").status_code == 409
    assert client.post("/api/mission/start").status_code == 409
    assert client.post("/api/mission/load", json={"mission_id": mid}).json()["state"] == "READY"
    r = client.post("/api/mission/start")
    assert r.status_code == 409 and "disarmed" in str(r.json()["error"]["details"])
    assert client.delete(f"/api/missions/{mid}").status_code == 409  # loaded
    client.post("/api/mission/unload")
    assert client.delete(f"/api/missions/{mid}").status_code == 204
    assert client.get(f"/api/missions/{mid}").status_code == 404


def test_arm_estop_reset_cycle(client):
    assert client.post("/api/safety/arm").json()["armed"]
    assert client.post("/api/safety/estop").json()["estop_latched"]
    r = client.post("/api/safety/arm")
    assert r.status_code == 409 and r.json()["error"]["code"] == "arming_refused"
    assert client.post("/api/safety/estop/reset").json() == {"estop_latched": False, "armed": False}


def test_ws_manual_control_deadman_and_disconnect(client):
    client.post("/api/safety/arm")
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"type": "acquire", "client_id": "test"})
        assert ws.receive_json()["type"] == "acquired"
        ws.send_json({"type": "drive", "seq": 1, "direction": "forward", "speed": 0.5})
        ack = ws.receive_json()
        assert ack["accepted"] and ack["left"] == 0.5
        time.sleep(0.15)
        assert client.get("/api/status").json()["motion"]["target"]["left"] > 0
        # stale sequence
        ws.send_json({"type": "drive", "seq": 1, "direction": "forward", "speed": 0.5})
        assert ws.receive_json()["code"] == "stale_command"
        # heartbeats stop -> dead-man
        time.sleep(0.4)
        motion = client.get("/api/status").json()["motion"]
        assert motion["target"] == {"left": 0.0, "right": 0.0}
        ws.send_json({"type": "drive", "seq": 2, "direction": "forward", "speed": 0.5})
        ws.receive_json()
    # socket closed -> session released and motors at zero
    time.sleep(0.1)
    st = client.get("/api/status").json()
    assert st["manual"]["session_active"] is False
    assert st["motion"]["owner"] == "none"
    assert st["motion"]["target"] == {"left": 0.0, "right": 0.0}


def test_ws_drive_rejected_when_disarmed(client):
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"type": "acquire"})
        ws.receive_json()
        ws.send_json({"type": "drive", "seq": 1, "direction": "forward", "speed": 0.5})
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "motion_not_permitted"
        ws.send_json({"type": "nonsense"})
        assert ws.receive_json()["code"] == "unknown_type"
        ws.send_text("{not json")
        assert ws.receive_json()["code"] == "bad_json"


def test_ws_estop(client):
    client.post("/api/safety/arm")
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"type": "estop", "seq": 1})
        assert ws.receive_json()["estop"]
    assert client.get("/api/status").json()["safety"]["estop_latched"]


def test_telemetry_ws(client):
    with client.websocket_connect("/ws/telemetry") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["system"]["mode"] == "simulation"
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "telemetry":
                assert "capabilities" in msg and "sensors" in msg
                break
        else:
            pytest.fail("no telemetry received")


def test_sim_controls(client):
    r = client.post("/api/sim/faults", json={"fault": "gps_loss", "active": True})
    assert "gps_loss" in r.json()["faults"]
    assert client.post("/api/sim/faults", json={"fault": "bogus", "active": True}).status_code == 422
    assert client.post("/api/sim/speed", json={"speed_factor": 4}).json()["speed_factor"] == 4


def test_boundaries_crud(client):
    pts = [{"lat": 1.0, "lon": 1.0}, {"lat": 1.0001, "lon": 1.0}, {"lat": 1.0001, "lon": 1.0001}]
    b = client.post("/api/boundaries", json={"name": "field", "points": pts}).json()
    assert client.put(f"/api/boundaries/{b['id']}", json={"name": "field2", "points": pts}).json()["name"] == "field2"
    assert len(client.get("/api/boundaries").json()) == 1
    assert client.delete(f"/api/boundaries/{b['id']}").status_code == 204


def test_auth_required_when_enabled(tmp_path):
    cfg = make_config(auth={"enabled": True, "token": "abc"})
    with TestClient(create_app(cfg, str(tmp_path / "a.db"))) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer abc"}).status_code == 200
        assert c.get("/api/status?token=wrong").status_code == 401
