"""Shared API dependencies: runtime access and optional bearer-token auth."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, WebSocket, status

from rover.runtime import RoverRuntime


def get_runtime(request: Request) -> RoverRuntime:
    return request.app.state.runtime


def _token_ok(rt: RoverRuntime, supplied: str | None) -> bool:
    if not rt.config.auth.enabled:
        return True
    expected = rt.config.auth.token or ""
    return bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())


def require_auth(request: Request) -> None:
    rt = get_runtime(request)
    if not rt.config.auth.enabled:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token")
    if not _token_ok(rt, supplied):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"code": "unauthorized", "message": "missing or invalid token"})


def ws_authorized(ws: WebSocket) -> bool:
    rt: RoverRuntime = ws.app.state.runtime
    return _token_ok(rt, ws.query_params.get("token"))
