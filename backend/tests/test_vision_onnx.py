"""End-to-end check of the ONNX YOLOv8 path with a synthetic model of the real output shape.

The model ignores image content (it adds 0 * mean(input)) and always emits one
box of class 'drill' at the centre, so the full pipeline — onnxruntime session,
fixed input size, metadata class names, letterbox, decode, NMS, un-letterbox —
is exercised without downloading real weights.
"""

import io

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from onnx import TensorProto, helper, numpy_helper  # noqa: E402

from rover.vision.detector import OnnxYoloDetector  # noqa: E402


def _make_model(path, imgsz=320, names=("person", "drill")):
    nc = len(names)
    n = 2100
    const = np.zeros((1, 4 + nc, n), dtype=np.float32)
    const[0, :4, 0] = [160, 160, 80, 40]  # centre box in input pixels
    const[0, 4 + 1, 0] = 0.92  # class 1 = drill
    nodes = [
        helper.make_node("ReduceMean", ["images"], ["m"], keepdims=0),
        helper.make_node("Mul", ["m", "zero"], ["z"]),
        helper.make_node("Add", ["base", "z"], ["output0"]),
    ]
    graph = helper.make_graph(
        nodes, "fake_yolo",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, imgsz, imgsz])],
        [helper.make_tensor_value_info("output0", TensorProto.FLOAT, [1, 4 + nc, n])],
        initializer=[numpy_helper.from_array(const, "base"), numpy_helper.from_array(np.array(0, dtype=np.float32), "zero")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    meta = model.metadata_props.add()
    meta.key, meta.value = "names", str({i: n for i, n in enumerate(names)})
    onnx.save(model, path)


def test_onnx_pipeline_end_to_end(tmp_path):
    from PIL import Image

    path = tmp_path / "fake.onnx"
    _make_model(str(path))
    det = OnnxYoloDetector(str(path), imgsz=640, conf=0.3, iou=0.45, threads=1, class_names=None)
    det.load()
    assert det.imgsz == 320  # fixed-shape export overrides config
    assert det.class_names == ["person", "drill"]
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (90, 90, 90)).save(buf, format="JPEG")
    res = det.detect(buf.getvalue())
    assert res.width == 640 and res.height == 480
    assert len(res.detections) == 1
    d = res.detections[0]
    assert d.label == "drill" and d.confidence == pytest.approx(0.92)
    x1, y1, x2, y2 = d.box
    assert (x1 + x2) / 2 == pytest.approx(0.5, abs=0.01) and (y1 + y2) / 2 == pytest.approx(0.5, abs=0.01)
    assert res.inference_ms >= 0


async def test_vision_service_on_simulated_camera(tmp_path, runtime_factory, config_factory):
    from .conftest import wait_until

    path = tmp_path / "fake.onnx"
    _make_model(str(path))
    cfg = config_factory(
        camera={"provider": "simulated"},
        vision={"enabled": True, "model_path": str(path), "max_rate_hz": 10, "event_classes": ["drill"],
                "event_min_confidence": 0.5, "event_min_interval_s": 60},
    )
    rt = await runtime_factory(cfg)
    assert await wait_until(lambda: rt.vision.latest is not None, timeout=8)
    assert rt.vision.status()["ready"] and rt.vision.status()["on_simulated_camera"]
    assert await wait_until(lambda: bool(rt.repo.list_detections()), timeout=5)
    events = rt.repo.list_detections()
    assert len(events) == 1  # rate-limited by event_min_interval_s
    ev = events[0]
    assert ev["source_kind"] == "camera" and ev["label"] == "drill"
    assert ev["simulated"] is True  # simulated camera -> simulated event
    assert ev["data"]["uncertainty"]["validated"] is False
    caps = {c["id"]: c for c in rt.capabilities()}
    assert caps["object_detection"]["status"] == "simulated"
