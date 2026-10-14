"""Camera object detection with YOLOv8n.

Two interchangeable backends implement :class:`FrameDetector`:

* ``onnx`` (default, recommended on the Pi 5): ONNX Runtime on CPU with a
  ``yolov8n.onnx`` export. Only needs ``onnxruntime``, ``numpy`` and ``Pillow``.
* ``ultralytics``: the Ultralytics package (pulls in PyTorch; heavier).

IMPORTANT: stock YOLOv8n weights are trained on COCO (people, vehicles, bottles,
...). They cannot detect buried objects or landmines. Treat outputs as unverified
camera object candidates unless custom weights have been trained and validated
for the task (``vision.weights_validated_for_task``).
"""

from __future__ import annotations

import ast
import io
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class Detection:
    label: str
    class_id: int
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 normalised to [0, 1]


@dataclass
class DetectionResult:
    model: str
    backend: str
    detections: list[Detection]
    inference_ms: float
    width: int
    height: int
    frame_seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for det in d["detections"]:
            det["confidence"] = round(det["confidence"], 3)
            det["box"] = [round(v, 4) for v in det["box"]]
        d["inference_ms"] = round(self.inference_ms, 1)
        return d


class DetectorError(RuntimeError):
    pass


class FrameDetector(ABC):
    backend = "abstract"

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def detect(self, jpeg: bytes) -> DetectionResult: ...

    class_names: list[str] = []


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Greedy non-maximum suppression. boxes: (N, 4) xyxy."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= iou]
    return keep


def letterbox(img: Any, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    from PIL import Image

    w, h = img.size
    r = min(size / w, size / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    padx, pady = (size - nw) // 2, (size - nh) // 2
    canvas.paste(resized, (padx, pady))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    return np.ascontiguousarray(arr.transpose(2, 0, 1)[None]), r, (padx, pady)


def decode_yolov8(
    output: np.ndarray, conf: float, iou: float, ratio: float, pad: tuple[float, float],
    width: int, height: int, names: list[str], max_det: int = 100,
) -> list[Detection]:
    """Decode a YOLOv8 head output of shape (1, 4 + nc, N) into detections."""
    pred = np.squeeze(output, 0)
    # Standard YOLOv8 export: (4 + nc, N). Use the class count when known.
    if (names and pred.shape[0] == 4 + len(names)) or (not names and pred.shape[0] < pred.shape[1]):
        pred = pred.T  # -> (N, 4 + nc)
    boxes_cxcywh = pred[:, :4]
    cls_scores = pred[:, 4:]
    if cls_scores.size == 0:
        return []
    class_ids = cls_scores.argmax(1)
    scores = cls_scores[np.arange(len(class_ids)), class_ids]
    mask = scores >= conf
    if not mask.any():
        return []
    b, s, c = boxes_cxcywh[mask], scores[mask], class_ids[mask]
    xyxy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1)
    # Class-aware NMS by offsetting boxes per class.
    offset = c[:, None].astype(np.float32) * 10000.0
    keep = nms(xyxy + offset, s, iou)[:max_det]
    out: list[Detection] = []
    for i in keep:
        x1, y1, x2, y2 = xyxy[i]
        x1, x2 = (x1 - pad[0]) / ratio, (x2 - pad[0]) / ratio
        y1, y2 = (y1 - pad[1]) / ratio, (y2 - pad[1]) / ratio
        box = (
            float(np.clip(x1 / width, 0, 1)), float(np.clip(y1 / height, 0, 1)),
            float(np.clip(x2 / width, 0, 1)), float(np.clip(y2 / height, 0, 1)),
        )
        cid = int(c[i])
        out.append(Detection(names[cid] if cid < len(names) else f"class_{cid}", cid, float(s[i]), box))
    return out


def parse_names(meta: dict[str, str] | None) -> list[str] | None:
    if not meta or "names" not in meta:
        return None
    try:
        names = ast.literal_eval(meta["names"])
    except (ValueError, SyntaxError):
        return None
    if isinstance(names, dict):
        return [str(names[k]) for k in sorted(names)]
    if isinstance(names, list):
        return [str(n) for n in names]
    return None


class OnnxYoloDetector(FrameDetector):
    backend = "onnx"

    def __init__(self, model_path: str, imgsz: int, conf: float, iou: float, threads: int, class_names: list[str] | None) -> None:
        self.model_path = model_path
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou
        self.threads = threads
        self.class_names = class_names or []
        self.session: Any = None
        self.input_name = ""

    def load(self) -> None:
        if not Path(self.model_path).is_file():
            raise DetectorError(f"model file not found: {self.model_path} (see docs/CAMERA_VISION.md to export yolov8n.onnx)")
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DetectorError("onnxruntime is not installed (pip install onnxruntime)") from exc
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(self.model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape
        if isinstance(shape[2], int) and shape[2] > 0:
            self.imgsz = int(shape[2])  # fixed-shape export overrides config
        if not self.class_names:
            meta = self.session.get_modelmeta().custom_metadata_map
            self.class_names = parse_names(meta) or []

    def detect(self, jpeg: bytes) -> DetectionResult:
        from PIL import Image

        if self.session is None:
            raise DetectorError("model not loaded")
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        w, h = img.size
        tensor, ratio, pad = letterbox(img, self.imgsz)
        t0 = time.perf_counter()
        output = self.session.run(None, {self.input_name: tensor})[0]
        ms = (time.perf_counter() - t0) * 1000
        dets = decode_yolov8(output, self.conf, self.iou, ratio, pad, w, h, self.class_names)
        return DetectionResult(Path(self.model_path).name, self.backend, dets, ms, w, h)


class UltralyticsYoloDetector(FrameDetector):
    backend = "ultralytics"

    def __init__(self, model_path: str, imgsz: int, conf: float, iou: float) -> None:
        self.model_path = model_path
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou
        self.model: Any = None

    def load(self) -> None:
        try:
            from ultralytics import YOLO  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DetectorError("ultralytics is not installed") from exc
        if not Path(self.model_path).is_file():
            raise DetectorError(f"model file not found: {self.model_path}")
        self.model = YOLO(self.model_path)
        self.class_names = [str(v) for _, v in sorted(self.model.names.items())]

    def detect(self, jpeg: bytes) -> DetectionResult:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        t0 = time.perf_counter()
        res = self.model.predict(img, imgsz=self.imgsz, conf=self.conf, iou=self.iou, verbose=False)[0]
        ms = (time.perf_counter() - t0) * 1000
        dets = []
        for xyxyn, cls, conf in zip(res.boxes.xyxyn.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            cid = int(cls)
            dets.append(Detection(self.class_names[cid] if cid < len(self.class_names) else f"class_{cid}", cid, float(conf), tuple(xyxyn)))  # type: ignore[arg-type]
        return DetectionResult(Path(self.model_path).name, self.backend, dets, ms, img.size[0], img.size[1])
