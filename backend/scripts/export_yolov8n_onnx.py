"""Export YOLOv8n weights to ONNX for the backend's onnx vision backend.

Run on a development machine (not required on the Pi):

    pip install ultralytics
    python scripts/export_yolov8n_onnx.py --weights yolov8n.pt --imgsz 320 --out models/yolov8n.onnx

Use your own trained weights (e.g. runs/detect/train/weights/best.pt) to export a
custom model; class names are embedded in the ONNX metadata and picked up
automatically. Copy the .onnx file to backend/models/ on the Pi and set
``vision.enabled: true`` in the configuration.

A fixed input size (imgsz 320) keeps CPU inference on the Pi 5 in the low
hundreds of milliseconds. Measure on your own hardware; the backend reports the
actual inference time.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default="yolov8n.pt")
    ap.add_argument("--imgsz", type=int, default=320)
    ap.add_argument("--out", default="models/yolov8n.onnx")
    ap.add_argument("--opset", type=int, default=12)
    args = ap.parse_args()
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("ultralytics is required for export: pip install ultralytics") from exc
    model = YOLO(args.weights)
    exported = model.export(format="onnx", imgsz=args.imgsz, opset=args.opset, simplify=True, dynamic=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(exported, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB), classes: {len(model.names)}")


if __name__ == "__main__":
    main()
