"""Runs the YOLOv8n detector on the latest camera frame at a bounded rate.

Optional: if vision is disabled, the model is missing, or onnxruntime is not
installed, the service reports *unavailable* with the reason and the rest of
the rover keeps working. It never fabricates detections.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from rover.camera import CameraProvider
from rover.config import VisionConfig
from rover.core.enums import Severity
from rover.core.events import EventLog
from rover.detection import DetectionRecorder, ProcessedDetection
from rover.vision.detector import DetectionResult, FrameDetector, OnnxYoloDetector, UltralyticsYoloDetector

log = logging.getLogger(__name__)


class VisionService:
    def __init__(self, cfg: VisionConfig, camera: CameraProvider, recorder: DetectionRecorder | None, events: EventLog) -> None:
        self.cfg = cfg
        self.camera = camera
        self.recorder = recorder
        self.events = events
        self.detector: FrameDetector | None = None
        self.ready = False
        self.error: str | None = None if cfg.enabled else "disabled"
        self.latest: DetectionResult | None = None
        self.frames_processed = 0
        self._last_event: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def _build(self) -> FrameDetector:
        c = self.cfg
        if c.backend == "ultralytics":
            return UltralyticsYoloDetector(c.model_path, c.imgsz, c.confidence, c.iou)
        return OnnxYoloDetector(c.model_path, c.imgsz, c.confidence, c.iou, c.threads, c.class_names)

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        det = self._build()
        try:
            await asyncio.to_thread(det.load)
        except Exception as exc:
            self.error = str(exc)
            self.events.emit(Severity.WARNING, "vision", "vision.unavailable", f"Object detection unavailable: {exc}")
            return
        self.detector, self.ready, self.error = det, True, None
        self.events.emit(Severity.INFO, "vision", "vision.ready", f"YOLO detector loaded ({det.backend}, {len(det.class_names)} classes)")
        self._task = asyncio.create_task(self._run(), name="vision")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        assert self.detector is not None
        period = 1.0 / self.cfg.max_rate_hz
        last_seq = 0
        while True:
            frame = await self.camera.wait_frame(last_seq, timeout=5.0)
            if frame is None:
                continue
            last_seq = frame.seq
            t0 = time.monotonic()
            try:
                result = await asyncio.to_thread(self.detector.detect, frame.jpeg)
            except Exception as exc:
                self.error = f"inference failed: {exc}"
                await asyncio.sleep(1.0)
                continue
            result.frame_seq = frame.seq
            self.latest = result
            self.frames_processed += 1
            self.error = None
            self._maybe_events(result)
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _maybe_events(self, result: DetectionResult) -> None:
        if self.recorder is None or not self.cfg.event_classes:
            return
        now = time.monotonic()
        for det in result.detections:
            if det.label not in self.cfg.event_classes or det.confidence < self.cfg.event_min_confidence:
                continue
            if now - self._last_event.get(det.label, -1e9) < self.cfg.event_min_interval_s:
                continue
            self._last_event[det.label] = now
            self.recorder.record(
                ProcessedDetection(
                    sensor_id="camera", source_kind="camera", reading=None, confidence=round(det.confidence, 3),
                    label=det.label, uncertainty={"model": result.model, "validated": self.cfg.weights_validated_for_task},
                    raw={"box": list(det.box)},
                ),
                simulated=self.camera.simulated,
                extra={"frame_seq": result.frame_seq},
            )

    def status(self) -> dict[str, Any]:
        r = self.latest
        return {
            "enabled": self.cfg.enabled,
            "backend": self.cfg.backend,
            "model_path": self.cfg.model_path,
            "model_description": self.cfg.model_description,
            "weights_validated_for_task": self.cfg.weights_validated_for_task,
            "ready": self.ready,
            "error": self.error,
            "classes": len(self.detector.class_names) if self.detector else 0,
            "frames_processed": self.frames_processed,
            "inference_ms": round(r.inference_ms, 1) if r else None,
            "result_age_s": round(time.time() - r.ts, 2) if r else None,
            "on_simulated_camera": self.camera.simulated,
        }

    def latest_dict(self) -> dict[str, Any] | None:
        return self.latest.to_dict() if self.latest else None
