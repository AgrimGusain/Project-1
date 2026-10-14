"""WebSocket endpoints.

``/ws/telemetry`` pushes telemetry snapshots at ``server.telemetry_rate_hz`` and
operator events as they occur.

``/ws/control`` carries manual driving. Protocol (JSON messages):

  -> {"type": "acquire", "client_id": "tablet-1", "takeover": false}
  <- {"type": "acquired", "session_id": "...", "heartbeat_interval_s": 0.1, "deadman_timeout_s": 0.35}
  -> {"type": "drive", "seq": 12, "mode": "direction", "direction": "forward", "speed": 0.5}
  -> {"type": "drive", "seq": 13, "mode": "vector", "throttle": 0.6, "steer": -0.2, "speed": 0.7}
  -> {"type": "stop", "seq": 14}
  -> {"type": "release"}
  -> {"type": "estop"}
  <- {"type": "ack", "seq": 12, "accepted": true, "left": 0.5, "right": 0.5}
  <- {"type": "error", "code": "...", "message": "...", "seq": 12}

Drive messages must be re-sent every heartbeat interval while a control is held;
the server's lease expires after ``deadman_timeout_s`` otherwise. Closing the
socket stops the rover immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from rover.api.deps import ws_authorized
from rover.motion.controller import CommandRejected
from rover.runtime import RoverRuntime

log = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGE_BYTES = 2048


@router.websocket("/ws/telemetry")
async def telemetry_ws(ws: WebSocket) -> None:
    if not ws_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    rt: RoverRuntime = ws.app.state.runtime
    queue = rt.events.subscribe()
    period = 1.0 / rt.config.server.telemetry_rate_hz
    try:
        await ws.send_json({"type": "hello", "system": rt.system_info(),
                            "events": [e.to_dict() for e in rt.events.recent(50)]})
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        while True:
            timeout = max(0.0, next_t - loop.time())
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=timeout)
                await ws.send_json({"type": "event", "event": ev.to_dict()})
                continue
            except TimeoutError:
                pass
            await ws.send_json(rt.telemetry())
            next_t = loop.time() + period
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rt.events.unsubscribe(queue)


@router.websocket("/ws/control")
async def control_ws(ws: WebSocket) -> None:
    if not ws_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    rt: RoverRuntime = ws.app.state.runtime
    client = ws.client.host if ws.client else "unknown"
    session_id: str | None = None

    async def send(msg: dict[str, Any]) -> None:
        try:
            await ws.send_json(msg)
        except Exception:  # pragma: no cover - socket gone
            pass

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                await send({"type": "error", "code": "too_large", "message": "message too large"})
                continue
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError("not an object")
            except ValueError:
                await send({"type": "error", "code": "bad_json", "message": "invalid JSON"})
                continue
            mtype = msg.get("type")
            seq = msg.get("seq")
            try:
                if mtype == "acquire":
                    client_id = str(msg.get("client_id") or client)[:64]
                    if session_id is not None:
                        rt.manual.close_session(session_id, "re-acquire")
                    s = rt.manual.open_session(f"{client_id}@{client}", takeover=bool(msg.get("takeover")))
                    session_id = s.session_id
                    await send({"type": "acquired", "session_id": session_id,
                                "heartbeat_interval_s": rt.config.manual.heartbeat_interval_s,
                                "deadman_timeout_s": rt.config.manual.deadman_timeout_s,
                                "max_speed": rt.config.manual.max_speed})
                elif mtype == "drive":
                    if session_id is None:
                        raise CommandRejected("no_session", "acquire control first")
                    result = rt.manual.drive(session_id, seq, msg)
                    await send({"type": "ack", "seq": seq, **result})
                elif mtype == "stop":
                    if session_id is not None:
                        rt.manual.stop(session_id)
                    await send({"type": "ack", "seq": seq, "accepted": True, "left": 0.0, "right": 0.0})
                elif mtype == "release":
                    if session_id is not None:
                        rt.manual.close_session(session_id, "released by client")
                        session_id = None
                    await send({"type": "released"})
                elif mtype == "estop":
                    rt.supervisor.emergency_stop(f"operator@{client}", "operator e-stop (control link)")
                    await send({"type": "ack", "seq": seq, "accepted": True, "estop": True})
                elif mtype == "ping":
                    await send({"type": "pong", "t": msg.get("t")})
                else:
                    await send({"type": "error", "code": "unknown_type", "message": f"unknown message type {mtype!r}"})
            except CommandRejected as exc:
                await send({"type": "error", "code": exc.code, "message": exc.message, "seq": seq})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if session_id is not None:
            # Connection gone: stop immediately, never wait for the dead-man.
            rt.manual.close_session(session_id, "control connection closed")
