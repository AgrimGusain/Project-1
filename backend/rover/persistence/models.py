"""SQLAlchemy ORM models (SQLite). Times are stored as naive UTC."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)


class Mission(Base):
    __tablename__ = "missions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    operating_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    execution: Mapped[str | None] = mapped_column(String(24))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_reason: Mapped[str | None] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    config_fingerprint: Mapped[str | None] = mapped_column(String(32))


class MissionEvent(Base):
    __tablename__ = "mission_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str | None] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (Index("ix_mission_events_mission_ts", "mission_id", "ts"),)


class EventRecord(Base):
    """Operator event log (warnings and above, plus mission-related info)."""

    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(12), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    mission_id: Mapped[str | None] = mapped_column(String(36), index=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class FaultRecord(Base):
    __tablename__ = "fault_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    mission_id: Mapped[str | None] = mapped_column(String(36), index=True)
    raised_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)
    cleared_by: Mapped[str | None] = mapped_column(String(64))


class TelemetrySample(Base):
    """Aggregated telemetry (default 1 Hz while a mission is active)."""

    __tablename__ = "telemetry_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str | None] = mapped_column(String(36))
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("ix_telemetry_mission_ts", "mission_id", "ts"),)


class SensorSample(Base):
    __tablename__ = "sensor_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str | None] = mapped_column(String(36))
    sensor_id: Mapped[str] = mapped_column(String(48), nullable=False)
    sensor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(120))
    __table_args__ = (Index("ix_sensor_samples_mission_ts", "mission_id", "ts"),)


class DetectionEvent(Base):
    __tablename__ = "detection_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)  # metal_detector | camera
    sensor_id: Mapped[str] = mapped_column(String(48), nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    reading: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    position: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    localization_quality: Mapped[str] = mapped_column(String(32), nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unreviewed")
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SurveyBoundary(Base):
    __tablename__ = "survey_boundaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    points: Mapped[list[dict[str, float]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ConfigMetadata(Base):
    __tablename__ = "config_metadata"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
