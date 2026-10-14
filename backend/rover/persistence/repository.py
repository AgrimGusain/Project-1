"""Data-access functions. All writes are short transactions serialised by a lock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from rover.core.clock import iso
from rover.core.enums import MissionStatus, Severity
from rover.core.events import Fault, LogEvent
from rover.persistence.db import Database
from rover.persistence.models import (
    ConfigMetadata,
    DetectionEvent,
    EventRecord,
    FaultRecord,
    Mission,
    MissionEvent,
    SensorSample,
    SurveyBoundary,
    TelemetrySample,
)

ACTIVE_STATUSES = {MissionStatus.ACTIVE.value}


def naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return iso(dt.replace(tzinfo=timezone.utc)) if dt else None


def mission_to_dict(m: Mission, *, include_plan: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": m.id,
        "name": m.name,
        "kind": m.kind,
        "operating_mode": m.operating_mode,
        "status": m.status,
        "execution": m.execution,
        "params": m.params,
        "plan_hash": m.plan_hash,
        "summary": m.summary,
        "progress": m.progress,
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
        "started_at": _iso(m.started_at),
        "paused_at": _iso(m.paused_at),
        "resumed_at": _iso(m.resumed_at),
        "completed_at": _iso(m.completed_at),
        "cancelled_at": _iso(m.cancelled_at),
        "ended_reason": m.ended_reason,
        "review_required": m.review_required,
        "reviewed_at": _iso(m.reviewed_at),
        "review_note": m.review_note,
    }
    if include_plan:
        d["plan"] = m.plan
    return d


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ missions

    def create_mission(self, **fields: Any) -> dict[str, Any]:
        now = now_naive()
        with self.db.write_lock, self.db.session() as s, s.begin():
            m = Mission(created_at=now, updated_at=now, **fields)
            s.add(m)
        return mission_to_dict(m)

    def get_mission(self, mission_id: str, include_plan: bool = True) -> dict[str, Any] | None:
        with self.db.session() as s:
            m = s.get(Mission, mission_id)
            return mission_to_dict(m, include_plan=include_plan) if m else None

    def list_missions(self, limit: int = 50, offset: int = 0, status: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self.db.session() as s:
            q = select(Mission)
            cq = select(func.count()).select_from(Mission)
            if status:
                q = q.where(Mission.status == status)
                cq = cq.where(Mission.status == status)
            rows = s.scalars(q.order_by(Mission.created_at.desc()).limit(limit).offset(offset)).all()
            total = int(s.scalar(cq) or 0)
            return [mission_to_dict(m, include_plan=False) for m in rows], total

    def update_mission(self, mission_id: str, **fields: Any) -> dict[str, Any] | None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            m = s.get(Mission, mission_id)
            if m is None:
                return None
            for k, v in fields.items():
                setattr(m, k, naive(v) if isinstance(v, datetime) else v)
            m.updated_at = now_naive()
        return mission_to_dict(m)

    def delete_mission(self, mission_id: str) -> bool:
        with self.db.write_lock, self.db.session() as s, s.begin():
            m = s.get(Mission, mission_id)
            if m is None:
                return False
            s.execute(delete(MissionEvent).where(MissionEvent.mission_id == mission_id))
            s.delete(m)
        return True

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """Mark missions left ACTIVE by a crash/reboot as INTERRUPTED; never resume them."""
        out = []
        with self.db.write_lock, self.db.session() as s, s.begin():
            rows = s.scalars(select(Mission).where(Mission.status.in_(ACTIVE_STATUSES))).all()
            now = now_naive()
            for m in rows:
                m.status = MissionStatus.INTERRUPTED.value
                m.review_required = True
                m.ended_reason = "backend stopped unexpectedly while the mission was active; operator review required"
                m.updated_at = now
                s.add(MissionEvent(mission_id=m.id, ts=now, type="interrupted", message=m.ended_reason))
                out.append(mission_to_dict(m, include_plan=False))
        return out

    def add_mission_event(
        self, mission_id: str, type_: str, message: str = "", from_state: str | None = None,
        to_state: str | None = None, data: dict[str, Any] | None = None,
    ) -> None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            s.add(MissionEvent(mission_id=mission_id, ts=now_naive(), type=type_, message=message,
                               from_state=from_state, to_state=to_state, data=data))

    def mission_events(self, mission_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(
                select(MissionEvent).where(MissionEvent.mission_id == mission_id).order_by(MissionEvent.id).limit(limit)
            ).all()
            return [
                {"id": r.id, "ts": _iso(r.ts), "type": r.type, "from_state": r.from_state, "to_state": r.to_state,
                 "message": r.message, "data": r.data}
                for r in rows
            ]

    # ------------------------------------------------------------------ event log & faults

    def persist_event(self, ev: LogEvent) -> None:
        if ev.level == Severity.INFO and ev.mission_id is None and not ev.code.startswith(("safety.", "mission.")):
            return  # keep routine info out of the database
        with self.db.write_lock, self.db.session() as s, s.begin():
            s.add(EventRecord(ts=naive(ev.ts), level=ev.level.value, source=ev.source, code=ev.code,
                              message=ev.message, mission_id=ev.mission_id, data=ev.data or None))

    def list_events(self, limit: int = 200, mission_id: str | None = None, min_level: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(EventRecord)
            if mission_id:
                q = q.where(EventRecord.mission_id == mission_id)
            if min_level:
                levels = [lv.value for lv in Severity if lv.rank >= Severity(min_level).rank]
                q = q.where(EventRecord.level.in_(levels))
            rows = s.scalars(q.order_by(EventRecord.id.desc()).limit(limit)).all()
            return [
                {"id": r.id, "ts": _iso(r.ts), "level": r.level, "source": r.source, "code": r.code,
                 "message": r.message, "mission_id": r.mission_id, "data": r.data}
                for r in rows
            ]

    def persist_fault_raised(self, fault: Fault, mission_id: str | None) -> None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            s.add(FaultRecord(code=fault.code, severity=fault.severity.value, source=fault.source,
                              message=fault.message, mission_id=mission_id, raised_at=naive(fault.first_seen)))

    def persist_fault_cleared(self, fault: Fault, by: str) -> None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            row = s.scalars(
                select(FaultRecord).where(FaultRecord.code == fault.code, FaultRecord.cleared_at.is_(None))
                .order_by(FaultRecord.id.desc())
            ).first()
            if row:
                row.cleared_at = now_naive()
                row.cleared_by = by

    def list_faults(self, limit: int = 200, mission_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(FaultRecord)
            if mission_id:
                q = q.where(FaultRecord.mission_id == mission_id)
            rows = s.scalars(q.order_by(FaultRecord.id.desc()).limit(limit)).all()
            return [
                {"id": r.id, "code": r.code, "severity": r.severity, "source": r.source, "message": r.message,
                 "mission_id": r.mission_id, "raised_at": _iso(r.raised_at), "cleared_at": _iso(r.cleared_at),
                 "cleared_by": r.cleared_by}
                for r in rows
            ]

    # ------------------------------------------------------------------ telemetry & sensors

    def add_telemetry(self, mission_id: str | None, data: dict[str, Any]) -> None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            s.add(TelemetrySample(mission_id=mission_id, ts=now_naive(), data=data))

    def mission_telemetry(self, mission_id: str, max_points: int = 2000) -> list[dict[str, Any]]:
        with self.db.session() as s:
            total = int(s.scalar(select(func.count()).select_from(TelemetrySample).where(TelemetrySample.mission_id == mission_id)) or 0)
            stride = max(1, -(-total // max_points))
            rows = s.scalars(
                select(TelemetrySample).where(TelemetrySample.mission_id == mission_id).order_by(TelemetrySample.id)
            ).all()
            return [{"ts": _iso(r.ts), **r.data} for i, r in enumerate(rows) if i % stride == 0]

    def add_sensor_samples(self, mission_id: str | None, samples: list[dict[str, Any]]) -> None:
        if not samples:
            return
        with self.db.write_lock, self.db.session() as s, s.begin():
            for smp in samples:
                s.add(SensorSample(mission_id=mission_id, ts=now_naive(), **smp))

    def mission_sensor_samples(self, mission_id: str, sensor_id: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(SensorSample).where(SensorSample.mission_id == mission_id)
            if sensor_id:
                q = q.where(SensorSample.sensor_id == sensor_id)
            rows = s.scalars(q.order_by(SensorSample.id).limit(limit)).all()
            return [
                {"ts": _iso(r.ts), "sensor_id": r.sensor_id, "sensor_type": r.sensor_type, "valid": r.valid,
                 "simulated": r.simulated, "value": r.value, "error": r.error}
                for r in rows
            ]

    def prune(self, retention_days: int) -> int:
        cutoff = now_naive() - timedelta(days=retention_days)
        with self.db.write_lock, self.db.session() as s, s.begin():
            n = s.execute(delete(TelemetrySample).where(TelemetrySample.ts < cutoff)).rowcount or 0
            n += s.execute(delete(SensorSample).where(SensorSample.ts < cutoff)).rowcount or 0
            n += s.execute(delete(EventRecord).where(EventRecord.ts < cutoff, EventRecord.level == "info")).rowcount or 0
        return int(n)

    # ------------------------------------------------------------------ detections

    def add_detection(self, **fields: Any) -> dict[str, Any]:
        with self.db.write_lock, self.db.session() as s, s.begin():
            row = DetectionEvent(ts=now_naive(), **fields)
            s.add(row)
        return self._det(row)

    @staticmethod
    def _det(r: DetectionEvent) -> dict[str, Any]:
        return {
            "id": r.id, "mission_id": r.mission_id, "ts": _iso(r.ts), "source_kind": r.source_kind,
            "sensor_id": r.sensor_id, "simulated": r.simulated, "label": r.label, "reading": r.reading,
            "confidence": r.confidence, "data": r.data, "position": r.position,
            "localization_quality": r.localization_quality, "review_status": r.review_status,
            "review_note": r.review_note, "reviewed_at": _iso(r.reviewed_at),
        }

    def list_detections(self, mission_id: str | None = None, limit: int = 500, review_status: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as s:
            q = select(DetectionEvent)
            if mission_id:
                q = q.where(DetectionEvent.mission_id == mission_id)
            if review_status:
                q = q.where(DetectionEvent.review_status == review_status)
            return [self._det(r) for r in s.scalars(q.order_by(DetectionEvent.id.desc()).limit(limit)).all()]

    def review_detection(self, det_id: int, status: str, note: str | None) -> dict[str, Any] | None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            r = s.get(DetectionEvent, det_id)
            if r is None:
                return None
            r.review_status = status
            r.review_note = note
            r.reviewed_at = now_naive()
        return self._det(r)

    # ------------------------------------------------------------------ boundaries

    @staticmethod
    def _bnd(b: SurveyBoundary) -> dict[str, Any]:
        return {"id": b.id, "name": b.name, "points": b.points, "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at)}

    def list_boundaries(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            return [self._bnd(b) for b in s.scalars(select(SurveyBoundary).order_by(SurveyBoundary.name)).all()]

    def save_boundary(self, name: str, points: list[dict[str, float]], boundary_id: int | None = None) -> dict[str, Any] | None:
        now = now_naive()
        with self.db.write_lock, self.db.session() as s, s.begin():
            if boundary_id is not None:
                b = s.get(SurveyBoundary, boundary_id)
                if b is None:
                    return None
                b.name, b.points, b.updated_at = name, points, now
            else:
                b = SurveyBoundary(name=name, points=points, created_at=now, updated_at=now)
                s.add(b)
        return self._bnd(b)

    def delete_boundary(self, boundary_id: int) -> bool:
        with self.db.write_lock, self.db.session() as s, s.begin():
            b = s.get(SurveyBoundary, boundary_id)
            if b is None:
                return False
            s.delete(b)
        return True

    # ------------------------------------------------------------------ config metadata

    def set_meta(self, key: str, value: Any) -> None:
        with self.db.write_lock, self.db.session() as s, s.begin():
            row = s.get(ConfigMetadata, key)
            if row is None:
                s.add(ConfigMetadata(key=key, value=value, updated_at=now_naive()))
            else:
                row.value, row.updated_at = value, now_naive()

    def get_meta(self, key: str) -> Any:
        with self.db.session() as s:
            row = s.get(ConfigMetadata, key)
            return row.value if row else None
