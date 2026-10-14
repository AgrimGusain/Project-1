"""REST endpoints. Handlers validate input and delegate; they never drive motors directly."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from rover.api.deps import get_runtime, require_auth
from rover.core.enums import ReviewStatus
from rover.planning.models import GeoPoint, GpsPlanRequest, GridPlanRequest
from rover.planning.service import plan_gps, plan_grid
from rover.runtime import RoverRuntime
from rover.safety.supervisor import checks_to_dicts

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
public = APIRouter(prefix="/api")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReasonBody(_Body):
    reason: str | None = Field(None, max_length=200)


class MissionCreate(_Body):
    name: str = Field(..., min_length=1, max_length=120)
    kind: Literal["grid", "gps"]
    params: dict[str, Any]


class MissionUpdate(_Body):
    name: str | None = Field(None, min_length=1, max_length=120)
    params: dict[str, Any]


class LoadBody(_Body):
    mission_id: str = Field(..., min_length=1, max_length=64)


class ReviewBody(_Body):
    status: ReviewStatus
    note: str | None = Field(None, max_length=1000)


class AckBody(_Body):
    note: str | None = Field(None, max_length=1000)


class BoundaryBody(_Body):
    name: str = Field(..., min_length=1, max_length=120)
    points: list[GeoPoint] = Field(..., min_length=3, max_length=200)


class SimSpeedBody(_Body):
    speed_factor: float = Field(..., ge=0.1, le=20)


class SimFaultBody(_Body):
    fault: str
    active: bool


class SimPoseBody(_Body):
    x: float = Field(..., ge=-10000, le=10000)
    y: float = Field(..., ge=-10000, le=10000)
    heading_deg: float = Field(0.0, ge=-360, le=360)


Rt = Depends(get_runtime)


# ---------------------------------------------------------------- health / system


@public.get("/health")
async def health(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return {"ok": True, "mode": rt.config.mode.value, "simulated": rt.config.is_simulation,
            "auth_required": rt.config.auth.enabled, "control_loop_ok": rt.controller.loop_healthy}


@router.get("/system")
async def system(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.system_info()


@router.get("/status")
async def status(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.telemetry()


@router.get("/config")
async def config(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.config.redacted()


@router.get("/capabilities")
async def capabilities(rt: RoverRuntime = Rt) -> list[dict[str, Any]]:
    return rt.capabilities()


# ---------------------------------------------------------------- safety


@router.get("/safety/preflight")
async def preflight(rt: RoverRuntime = Rt) -> dict[str, Any]:
    checks = rt.supervisor.preflight()
    return {"ready": all(c.passed or not c.required for c in checks), "checks": checks_to_dicts(checks)}


@router.post("/safety/arm")
async def arm(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    checks = rt.supervisor.arm(by=_who(request))
    return {"armed": True, "simulated": rt.config.is_simulation, "checks": checks_to_dicts(checks)}


@router.post("/safety/disarm")
async def disarm(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.supervisor.disarm("operator request", by=_who(request))
    return {"armed": False}


@router.post("/safety/estop")
async def estop(request: Request, body: ReasonBody | None = None, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.supervisor.emergency_stop(f"operator ({_who(request)})", (body.reason if body and body.reason else "operator e-stop"))
    return {"estop_latched": True}


@router.post("/safety/estop/reset")
async def estop_reset(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.supervisor.reset_emergency_stop(by=_who(request))
    return {"estop_latched": rt.supervisor.estop_latched, "armed": rt.supervisor.armed}


@router.get("/faults")
async def faults(rt: RoverRuntime = Rt, history: bool = False, limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    out: dict[str, Any] = {"active": [f.to_dict() for f in rt.faults.active()]}
    if history:
        out["history"] = rt.repo.list_faults(limit)
    return out


@router.post("/faults/{code}/clear")
async def clear_fault(code: str, request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    if not rt.faults.clear(code, by=_who(request)):
        raise HTTPException(404, detail={"code": "not_found", "message": "fault not active"})
    return {"cleared": code, "note": "the fault will be raised again if the underlying condition persists"}


@router.get("/events")
async def events(rt: RoverRuntime = Rt, limit: int = Query(100, ge=1, le=1000), persisted: bool = False,
           mission_id: str | None = None, min_level: str | None = None) -> list[dict[str, Any]]:
    if persisted or mission_id:
        return rt.repo.list_events(limit, mission_id, min_level)
    return [e.to_dict() for e in reversed(rt.events.recent(limit))]


# ---------------------------------------------------------------- manual (REST fallback)


@router.post("/manual/stop")
async def manual_stop(rt: RoverRuntime = Rt) -> dict[str, Any]:
    """Always allowed: zero any manual command."""
    rt.manual.stop()
    return {"stopped": True}


# ---------------------------------------------------------------- planning


@router.post("/plan/grid")
async def preview_grid(req: GridPlanRequest, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return plan_grid(rt.config, req).model_dump(mode="json")


@router.post("/plan/gps")
async def preview_gps(req: GpsPlanRequest, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return plan_gps(rt.config, req).model_dump(mode="json")


# ---------------------------------------------------------------- missions


@router.get("/missions")
async def list_missions(rt: RoverRuntime = Rt, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  status: str | None = None) -> dict[str, Any]:
    items, total = rt.repo.list_missions(limit, offset, status)
    return {"items": items, "total": total}


@router.post("/missions", status_code=201)
async def create_mission(body: MissionCreate, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.missions.create(body.name, body.kind, body.params)


@router.get("/missions/{mission_id}")
async def get_mission(mission_id: str, rt: RoverRuntime = Rt) -> dict[str, Any]:
    m = rt.repo.get_mission(mission_id)
    if m is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "mission not found"})
    return m


@router.put("/missions/{mission_id}")
async def update_mission(mission_id: str, body: MissionUpdate, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.missions.replan(mission_id, body.name, body.params)


@router.delete("/missions/{mission_id}", status_code=204)
async def delete_mission(mission_id: str, rt: RoverRuntime = Rt) -> Response:
    rt.missions.delete(mission_id)
    return Response(status_code=204)


@router.post("/missions/{mission_id}/duplicate", status_code=201)
async def duplicate_mission(mission_id: str, rt: RoverRuntime = Rt) -> dict[str, Any]:
    m = rt.repo.get_mission(mission_id, include_plan=False)
    if m is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "mission not found"})
    params = dict(m["params"])
    params["execution_mode"] = rt.config.mode.value
    return rt.missions.create(f"{m['name']} (copy)"[:120], m["kind"], params)


@router.post("/missions/{mission_id}/acknowledge")
async def acknowledge_review(mission_id: str, body: AckBody | None = None, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.missions.acknowledge_review(mission_id, body.note if body else None)


@router.get("/missions/{mission_id}/events")
async def mission_events(mission_id: str, rt: RoverRuntime = Rt) -> dict[str, Any]:
    return {"transitions": rt.repo.mission_events(mission_id), "log": rt.repo.list_events(500, mission_id),
            "faults": rt.repo.list_faults(500, mission_id)}


@router.get("/missions/{mission_id}/telemetry")
async def mission_telemetry(mission_id: str, rt: RoverRuntime = Rt, max_points: int = Query(2000, ge=10, le=20000)) -> list[dict[str, Any]]:
    return rt.repo.mission_telemetry(mission_id, max_points)


@router.get("/missions/{mission_id}/sensors")
async def mission_sensors(mission_id: str, rt: RoverRuntime = Rt, sensor_id: str | None = None) -> list[dict[str, Any]]:
    return rt.repo.mission_sensor_samples(mission_id, sensor_id)


@router.get("/missions/{mission_id}/detections")
async def mission_detections(mission_id: str, rt: RoverRuntime = Rt) -> list[dict[str, Any]]:
    return rt.repo.list_detections(mission_id)


# ---------------------------------------------------------------- active mission control


@router.get("/mission/active")
async def active_mission(rt: RoverRuntime = Rt, trace: bool = False) -> dict[str, Any]:
    snap = rt.missions.snapshot(include_trace=trace)
    if rt.missions.active is not None:
        snap["plan"] = rt.missions.active.plan.model_dump(mode="json")
    return snap


@router.post("/mission/load")
async def load(body: LoadBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.missions.load(body.mission_id)
    return rt.missions.snapshot()


@router.post("/mission/unload")
async def unload(rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.missions.unload()
    return rt.missions.snapshot()


@router.post("/mission/start")
async def start(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    await rt.missions.start(by=_who(request))
    return rt.missions.snapshot()


@router.post("/mission/pause")
async def pause(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.missions.pause("operator request", by=_who(request))
    return rt.missions.snapshot()


@router.post("/mission/resume")
async def resume(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.missions.resume(by=_who(request))
    return rt.missions.snapshot()


@router.post("/mission/stop")
async def stop(request: Request, rt: RoverRuntime = Rt) -> dict[str, Any]:
    await rt.missions.stop(by=_who(request))
    return rt.missions.snapshot()


@router.post("/mission/acknowledge")
async def acknowledge(rt: RoverRuntime = Rt) -> dict[str, Any]:
    rt.missions.acknowledge()
    return rt.missions.snapshot()


# ---------------------------------------------------------------- sensors, localization, detections


@router.get("/sensors")
async def sensors(rt: RoverRuntime = Rt) -> list[dict[str, Any]]:
    return rt.sensors.snapshot()


@router.get("/localization")
async def localization(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.localization.snapshot()


@router.get("/detections")
async def detections(rt: RoverRuntime = Rt, review_status: str | None = None, limit: int = Query(200, ge=1, le=2000)) -> list[dict[str, Any]]:
    return rt.repo.list_detections(None, limit, review_status)


@router.patch("/detections/{det_id}")
async def review_detection(det_id: int, body: ReviewBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    out = rt.repo.review_detection(det_id, body.status.value, body.note)
    if out is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "detection not found"})
    return out


# ---------------------------------------------------------------- boundaries


@router.get("/boundaries")
async def boundaries(rt: RoverRuntime = Rt) -> list[dict[str, Any]]:
    return rt.repo.list_boundaries()


@router.post("/boundaries", status_code=201)
async def create_boundary(body: BoundaryBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    out = rt.repo.save_boundary(body.name, [p.model_dump() for p in body.points])
    assert out is not None
    return out


@router.put("/boundaries/{boundary_id}")
async def update_boundary(boundary_id: int, body: BoundaryBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    out = rt.repo.save_boundary(body.name, [p.model_dump() for p in body.points], boundary_id)
    if out is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "boundary not found"})
    return out


@router.delete("/boundaries/{boundary_id}", status_code=204)
async def delete_boundary(boundary_id: int, rt: RoverRuntime = Rt) -> Response:
    if not rt.repo.delete_boundary(boundary_id):
        raise HTTPException(404, detail={"code": "not_found", "message": "boundary not found"})
    return Response(status_code=204)


# ---------------------------------------------------------------- camera & vision


@router.get("/camera/status")
async def camera_status(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return rt.camera.status()


@router.get("/camera/snapshot")
async def camera_snapshot(rt: RoverRuntime = Rt) -> Response:
    f = rt.camera.latest
    if f is None or not rt.camera.online:
        raise HTTPException(503, detail={"code": "camera_offline", "message": rt.camera.error or "no frame available"})
    return Response(f.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/camera/stream")
async def camera_stream(request: Request, rt: RoverRuntime = Rt) -> StreamingResponse:
    boundary = "frame"
    min_period = 1.0 / rt.config.camera.proxy_fps

    async def gen():  # type: ignore[no-untyped-def]
        seq = 0
        while not await request.is_disconnected():
            f = await rt.camera.wait_frame(seq, timeout=5.0)
            if f is None:
                continue
            seq = f.seq
            yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(f.jpeg)}\r\n\r\n").encode() + f.jpeg + b"\r\n"
            await asyncio.sleep(min_period)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}",
                             headers={"Cache-Control": "no-store"})


@router.get("/vision/status")
async def vision_status(rt: RoverRuntime = Rt) -> dict[str, Any]:
    return {**rt.vision.status(), "latest": rt.vision.latest_dict()}


# ---------------------------------------------------------------- simulation


def _sim(rt: RoverRuntime) -> Any:
    if rt.world is None:
        raise HTTPException(409, detail={"code": "not_simulation", "message": "simulation controls are only available in simulation mode"})
    return rt.world


@router.get("/sim")
async def sim_state(rt: RoverRuntime = Rt) -> dict[str, Any]:
    _sim(rt)
    return rt.sim_snapshot() or {}


@router.post("/sim/speed")
async def sim_speed(body: SimSpeedBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    _sim(rt)
    rt.clock.set_scale(body.speed_factor)  # type: ignore[attr-defined]
    return rt.sim_snapshot() or {}


@router.post("/sim/faults")
async def sim_fault(body: SimFaultBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    world = _sim(rt)
    try:
        if body.active:
            world.inject_fault(body.fault)
        else:
            world.clear_fault(body.fault)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "unknown_fault", "message": str(exc)}) from exc
    rt.events.emit("warning" if body.active else "info", "simulation", "sim.fault",
                   f"Simulated fault '{body.fault}' {'injected' if body.active else 'cleared'}")
    return rt.sim_snapshot() or {}


@router.post("/sim/pose")
async def sim_pose(body: SimPoseBody, rt: RoverRuntime = Rt) -> dict[str, Any]:
    world = _sim(rt)
    if rt.supervisor.armed:
        raise HTTPException(409, detail={"code": "armed", "message": "disarm before moving the simulated rover"})
    world.reset(body.x, body.y, body.heading_deg)
    return rt.sim_snapshot() or {}


def _who(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return f"operator@{client}"
