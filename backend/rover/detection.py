"""Detection-event framework.

Raw measurements -> :class:`DetectionProcessor` -> candidate events -> persisted
:class:`DetectionEvent` rows awaiting human review.

A detection is a *candidate signal*, never proof that an object (let alone a
mine) is present. Confidence is a processor-defined signal strength in [0, 1].
Events carry a position only when a valid position source exists, and always
record the localization quality. Simulated sensors produce events flagged
``simulated=True``.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from rover.core.enums import Severity
from rover.core.events import EventLog
from rover.localization.base import LocalizationEstimate
from rover.sensors.base import SensorReading

log = logging.getLogger(__name__)


@dataclass
class ProcessedDetection:
    sensor_id: str
    source_kind: str
    reading: float | None
    confidence: float | None
    label: str | None = None
    uncertainty: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class DetectionProcessor(ABC):
    """Pluggable processing stage (e.g. a validated model or external detector)."""

    name = "abstract"

    @abstractmethod
    def process(self, reading: SensorReading) -> ProcessedDetection | None: ...


class ThresholdProcessor(DetectionProcessor):
    """Hysteresis threshold with a minimum interval between events."""

    name = "threshold_hysteresis"

    def __init__(self, threshold: float, release: float, saturation: float, min_interval_s: float) -> None:
        self.threshold = threshold
        self.release = release
        self.saturation = saturation
        self.min_interval_s = min_interval_s
        self.active = False
        self.last_event = -1e9

    def process(self, reading: SensorReading) -> ProcessedDetection | None:
        if not reading.valid:
            return None
        signal = reading.field("signal")
        if signal is None:
            return None
        signal = float(signal)
        if self.active:
            if signal < self.release:
                self.active = False
            return None
        if signal < self.threshold:
            return None
        self.active = True
        now = time.monotonic()
        if now - self.last_event < self.min_interval_s:
            return None
        self.last_event = now
        span = max(self.saturation - self.threshold, 1e-9)
        confidence = max(0.0, min(1.0, (signal - self.threshold) / span))
        return ProcessedDetection(
            sensor_id=reading.sensor_id, source_kind="metal_detector", reading=signal, confidence=round(confidence, 3),
            label="metal_signal", uncertainty={"method": self.name, "threshold": self.threshold}, raw=reading.value,
        )


def localization_quality(est: LocalizationEstimate) -> tuple[str, dict[str, Any] | None]:
    if not est.position_valid:
        return f"none ({est.provider}: {est.status.value})", None
    tag = "simulated_" if est.simulated else ""
    tag += est.provider + ("_experimental" if est.experimental else "")
    pos = {
        "frame": est.kind, "x": est.x, "y": est.y, "lat": est.lat, "lon": est.lon,
        "accuracy_m": est.horizontal_accuracy_m, "accuracy_source": est.accuracy_source, "provider": est.provider,
    }
    acc = est.horizontal_accuracy_m
    return f"{tag}" + (f" (±{acc:.2f} m)" if acc is not None else ""), pos


class DetectionRecorder:
    def __init__(self, repo: Any, events: EventLog, localization: Any, mission_id: Any) -> None:
        self.repo = repo
        self.events = events
        self.localization = localization
        self.mission_id = mission_id  # callable -> str | None
        self.processors: dict[str, DetectionProcessor] = {}
        self.recent: list[dict[str, Any]] = []

    def add_processor(self, sensor_id: str, processor: DetectionProcessor) -> None:
        self.processors[sensor_id] = processor

    def on_reading(self, reading: SensorReading) -> None:
        proc = self.processors.get(reading.sensor_id)
        if proc is None:
            return
        det = proc.process(reading)
        if det is not None:
            self.record(det, simulated=reading.simulated)

    def record(self, det: ProcessedDetection, simulated: bool, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        est = self.localization.estimate()
        quality, pos = localization_quality(est)
        mission_id = self.mission_id()
        try:
            row = self.repo.add_detection(
                mission_id=mission_id, source_kind=det.source_kind, sensor_id=det.sensor_id, simulated=simulated,
                label=det.label, reading=det.reading, confidence=det.confidence,
                data={"raw": det.raw, "uncertainty": det.uncertainty, **(extra or {})},
                position=pos, localization_quality=quality, review_status="unreviewed",
            )
        except Exception:  # pragma: no cover
            log.exception("failed to store detection event")
            return None
        prefix = "SIMULATED " if simulated else ""
        self.events.emit(
            Severity.WARNING, f"detection.{det.sensor_id}", "detection.candidate",
            f"{prefix}candidate {det.label or det.source_kind} signal (confidence {det.confidence}) — requires review",
            detection_id=row["id"],
        )
        self.recent = ([row] + self.recent)[:20]
        return row
