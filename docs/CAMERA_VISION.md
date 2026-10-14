# Camera and YOLOv8n detection

## Data flow

```
ESP32-CAM ──MJPEG :81/stream──► backend Esp32MjpegCamera (single client, reconnect with backoff)
                                   │ latest JPEG frame
                                   ├──► /api/camera/stream  (MJPEG re-served to any number of browsers)
                                   ├──► /api/camera/snapshot
                                   └──► VisionService ──► YOLOv8n (ONNX Runtime, CPU) at ≤ vision.max_rate_hz
                                                            └──► telemetry `vision.latest` (boxes drawn in the UI)
                                                            └──► detection events for `vision.event_classes` only
```

Firmware and flashing instructions are in `firmware/README.md`.

## Enabling YOLOv8n

1. On a development machine: `pip install ultralytics`, then
   `python backend/scripts/export_yolov8n_onnx.py --weights yolov8n.pt --imgsz 320 --out backend/models/yolov8n.onnx`
2. On the Pi: `backend/.venv/bin/pip install onnxruntime` (or `install_pi.sh --vision`).
3. Configure:

```yaml
vision:
  enabled: true
  backend: onnx
  model_path: models/yolov8n.onnx
  imgsz: 320          # a fixed-size export overrides this
  confidence: 0.35
  max_rate_hz: 2      # inference runs on the latest frame, at most this often
  threads: 2
  event_classes: []   # classes that create logged detection events (none by default)
  weights_validated_for_task: false
```

If the model file or onnxruntime is missing, the capability shows
*unavailable* with the reason. Everything else keeps working, and no detections
are fabricated. `backend: ultralytics` is supported but pulls in PyTorch, which
is heavy on a Pi.

The backend reports the measured inference time on the Camera page. Measure it
on your own Pi 5 rather than relying on published figures; it depends on
`imgsz`, thread count and cooling.

## Limitations (read before interpreting detections)

* **Stock YOLOv8n weights are trained on COCO** (people, vehicles, animals,
  household objects). They cannot detect buried objects, and ordinary camera
  footage cannot reliably reveal buried mines. Nothing in this system claims to.
* Custom weights trained on your own labelled dataset can be exported the same
  way. Class names are read from the ONNX metadata. Set
  `weights_validated_for_task: true` only after independent validation on held-out data.
* Confidence is the model's score, not a probability that a hazard exists.
  Camera events are stored as unverified candidates with `review_status: unreviewed`.
* Events from a simulated camera are flagged `simulated`.

## Adding another detector

Implement `rover.vision.detector.FrameDetector` (`load()`, `detect(jpeg) ->
DetectionResult`), or a `rover.detection.DetectionProcessor` for non-camera
signals (e.g. a validated metal-detector classifier). Results carry the raw
measurement, processed result, timestamp and uncertainty fields, and are
recorded through `DetectionRecorder`.
