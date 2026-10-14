"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rover import __version__
from rover.api import routes, ws
from rover.config import RoverConfig
from rover.mission.manager import MissionError
from rover.mission.state_machine import InvalidTransition
from rover.motion.controller import CommandRejected
from rover.planning.coverage import PlanningError
from rover.runtime import RoverRuntime
from rover.safety.supervisor import ArmingRefused, checks_to_dicts

log = logging.getLogger(__name__)


def _error(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


def create_app(config: RoverConfig, db_path: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = RoverRuntime(config, db_path)
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="Survey Rover Control API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(CommandRejected)
    async def _cmd(_: Request, exc: CommandRejected) -> JSONResponse:
        return _error(409, exc.code, exc.message)

    @app.exception_handler(MissionError)
    async def _mission(_: Request, exc: MissionError) -> JSONResponse:
        status = 404 if exc.code == "not_found" else 409
        return _error(status, exc.code, exc.message, details=exc.details)

    @app.exception_handler(InvalidTransition)
    async def _trans(_: Request, exc: InvalidTransition) -> JSONResponse:
        return _error(409, "invalid_transition", str(exc))

    @app.exception_handler(PlanningError)
    async def _plan(_: Request, exc: PlanningError) -> JSONResponse:
        return _error(422, "planning_failed", str(exc), issues=[i.model_dump() for i in exc.issues])

    @app.exception_handler(ArmingRefused)
    async def _arm(_: Request, exc: ArmingRefused) -> JSONResponse:
        return _error(409, "arming_refused", str(exc), checks=checks_to_dicts(exc.checks))

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]
        return _error(422, "validation_error", "request validation failed", fields=errs)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return _error(exc.status_code, str(exc.detail.get("code", "error")), str(exc.detail.get("message", "")))
        return _error(exc.status_code, "error", str(exc.detail))

    app.include_router(routes.public)
    app.include_router(routes.router)
    app.include_router(ws.router)

    dist = config.server.frontend_dist
    if dist:
        path = Path(dist)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if (path / "index.html").is_file():
            app.mount("/assets", StaticFiles(directory=path / "assets"), name="assets")

            @app.get("/{full_path:path}", include_in_schema=False)
            async def spa(full_path: str) -> FileResponse:
                if full_path.startswith(("api/", "ws/")):
                    raise HTTPException(404)
                candidate = (path / full_path).resolve()
                if full_path and candidate.is_file() and path in candidate.parents:
                    return FileResponse(candidate)
                return FileResponse(path / "index.html")
        else:
            log.info("frontend build not found at %s; serving API only", path)
    return app
