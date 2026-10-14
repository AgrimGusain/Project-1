"""Vision decoding, odometry, capability extensibility, persistence."""

import io
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from rover import capabilities
from rover.capabilities import CapabilityReport, capability, evaluate_all
from rover.persistence.db import Database
from rover.persistence.repository import Repository
from rover.sensors.base import SensorReading
from rover.vision.detector import OnnxYoloDetector, decode_yolov8, letterbox, nms, parse_names

# ---------------------------------------------------------------- vision


def test_nms_suppresses_overlaps():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [20, 20, 30, 30]], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    assert nms(boxes, scores, 0.5) == [0, 2]


def test_decode_yolov8_output_and_unletterbox():
    nc = 3
    out = np.zeros((1, 4 + nc, 5), dtype=np.float32)
    out[0, :4, 0] = [160, 160, 64, 32]  # centre of a 320 input
    out[0, 4 + 2, 0] = 0.9
    out[0, :4, 1] = [161, 160, 64, 32]  # duplicate, suppressed
    out[0, 4 + 2, 1] = 0.8
    out[0, 4 + 0, 2] = 0.1  # below threshold
    dets = decode_yolov8(out, 0.25, 0.45, ratio=0.5, pad=(0, 40), width=640, height=480, names=["a", "b", "c"])
    assert len(dets) == 1 and dets[0].label == "c"
    x1, y1, x2, y2 = dets[0].box
    assert x1 == pytest.approx((128 / 0.5) / 640) and y1 == pytest.approx(((144 - 40) / 0.5) / 480)


def test_letterbox_shape():
    from PIL import Image

    arr, r, pad = letterbox(Image.new("RGB", (640, 480)), 320)
    assert arr.shape == (1, 3, 320, 320) and r == 0.5 and pad == (0, 40)


def test_parse_names():
    assert parse_names({"names": "{0: 'person', 1: 'bicycle'}"}) == ["person", "bicycle"]
    assert parse_names({}) is None


def test_missing_model_reports_unavailable():
    from rover.vision.detector import DetectorError

    with pytest.raises(DetectorError, match="not found"):
        OnnxYoloDetector("does/not/exist.onnx", 320, 0.3, 0.45, 1, None).load()


async def test_vision_enabled_without_model_degrades_gracefully(runtime_factory, config_factory):
    rt = await runtime_factory(config_factory(vision={"enabled": True, "model_path": "nope.onnx"}))
    st = rt.vision.status()
    assert st["enabled"] and not st["ready"] and "not found" in st["error"]
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["object_detection"]["status"] == "unavailable"
    assert caps["manual_control"]["status"] in ("simulated", "unavailable")  # unaffected by vision


# ---------------------------------------------------------------- odometry


def _enc(side, counts, t):
    return SensorReading(f"{side}_enc", "wheel_encoder", None, t, {"counts": counts}, {"counts": "count"}, True, True,
                         metadata={"side": side})


def test_odometry_dead_reckoning_straight_and_turn():
    from rover.localization.local import OdometryProvider

    cfg = SimpleNamespace(side=None, counts_per_rev=100.0, wheel_diameter_m=1 / math.pi)  # 1 m per rev
    left = SimpleNamespace(cfg=SimpleNamespace(**{**vars(cfg), "side": "left"}), simulated=True)
    right = SimpleNamespace(cfg=SimpleNamespace(**{**vars(cfg), "side": "right"}), simulated=True)
    sensors = SimpleNamespace(of_type=lambda t: [left, right] if t == "wheel_encoder" else [], add_listener=lambda fn: None)
    odo = OdometryProvider(sensors, track_m=0.5)
    odo.reset_local_pose(0, 0, 0)
    t = time.monotonic()
    for i, (l, r) in enumerate([(0, 0), (100, 100), (200, 200)]):
        odo.on_reading(_enc("left", l, t + i * 0.1))
        odo.on_reading(_enc("right", r, t + i * 0.1))
    est = odo.estimate()
    assert est.position_valid and est.experimental
    assert est.x == pytest.approx(0, abs=1e-9) and est.y == pytest.approx(2.0)
    # spin in place clockwise: left +, right -  -> dtheta = (dl-dr)/track rad
    arc = 0.5 * math.pi / 4  # 45 deg each side ... total 90 deg
    odo.on_reading(_enc("left", 200 + int(arc * 100), t + 0.4))
    odo.on_reading(_enc("right", 200 - int(arc * 100), t + 0.4))
    assert odo.estimate().heading_deg == pytest.approx(90, abs=2.0)


def test_odometry_without_encoder_data_is_not_valid():
    from rover.localization.local import OdometryProvider

    sensors = SimpleNamespace(of_type=lambda t: [], add_listener=lambda fn: None)
    odo = OdometryProvider(sensors, track_m=0.5)
    assert not odo.estimate().position_valid


# ---------------------------------------------------------------- capabilities


def test_new_capability_registers_without_touching_others():
    before = len(capabilities._REGISTRY)

    @capability("test_lidar")
    def _lidar(rt):
        return CapabilityReport("test_lidar", "Lidar", "not_installed", "no lidar")

    try:
        rt = SimpleNamespace()
        reports = {r.id: r for r in evaluate_all(rt)}
        assert reports["test_lidar"].status == "not_installed"
        # other checks failing on the fake runtime are reported, not raised
        assert all(isinstance(r, CapabilityReport) for r in reports.values())
    finally:
        capabilities._REGISTRY[:] = capabilities._REGISTRY[:before]


async def test_capabilities_with_simulated_gps(runtime_factory, config_factory):
    from .conftest import wait_until

    rt = await runtime_factory(config_factory(localization={"provider": "simulated_gps", "gps": {"max_accuracy_m": 0.5}}))
    await wait_until(lambda: rt.localization.estimate().position_valid, timeout=3)
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["gps_navigation"]["status"] == "simulated"  # never "ready" for a simulated receiver
    rt.world.inject_fault("gps_loss")
    await wait_until(lambda: not rt.localization.estimate().position_valid, timeout=3)
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["gps_navigation"]["status"] == "unavailable"
    assert caps["gps_planning"]["status"] == "ready"


# ---------------------------------------------------------------- persistence


def test_migrations_idempotent(tmp_path):
    db = Database.for_path(str(tmp_path / "x.db"))
    assert db.migrate() == 1
    assert db.migrate() == 1
    repo = Repository(db)
    repo.set_meta("k", {"a": 1})
    assert repo.get_meta("k") == {"a": 1}
    db.close()


def test_newer_schema_refused(tmp_path):
    from sqlalchemy import text

    db = Database.for_path(str(tmp_path / "y.db"))
    db.migrate()
    with db.engine.begin() as c:
        c.execute(text("INSERT INTO schema_version (version, applied_at, description) VALUES (99, '2030-01-01', 'future')"))
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate()
    db.close()
