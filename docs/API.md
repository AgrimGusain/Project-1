# API reference

Interactive OpenAPI documentation is at `http://<rover>:8000/docs`.

**Auth.** When `auth.enabled`, send `Authorization: Bearer <token>`, or `?token=`
for WebSockets and `<img>` streams. `/api/health` is always public.

**Errors.** Every error has the form
`{"error": {"code": "...", "message": "...", ...details}}`.
Status codes: 401 auth, 404 not found, 409 state conflict (invalid transition,
not owner, arming refused, …), 422 validation or planning failure.

## REST

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | liveness, mode, auth requirement |
| GET | `/api/system` | version, mode, hardware errors, UI settings, calibration |
| GET | `/api/status` | full telemetry snapshot (same as the WebSocket) |
| GET | `/api/config` | effective configuration (secrets redacted) |
| GET | `/api/capabilities` | capability reports |
| GET | `/api/safety/preflight` | arming checks |
| POST | `/api/safety/arm` · `/disarm` · `/estop` · `/estop/reset` | safety commands |
| GET | `/api/faults?history=true` | active faults (+ history) |
| POST | `/api/faults/{code}/clear` | acknowledge a fault (re-raised if the condition persists) |
| GET | `/api/events` | recent operator events (`persisted=true` for the database log) |
| POST | `/api/manual/stop` | zero any manual command (always allowed) |
| POST | `/api/plan/grid` · `/api/plan/gps` | preview a plan (not saved) |
| GET/POST | `/api/missions` | list (paginated, `status=` filter) / create (`{name, kind, params}`) |
| GET/PUT/DELETE | `/api/missions/{id}` | get (with plan) / re-plan (planned only) / delete |
| POST | `/api/missions/{id}/duplicate` · `/acknowledge` | copy for re-run / mark an interrupted mission reviewed |
| GET | `/api/missions/{id}/events` · `/telemetry` · `/sensors` · `/detections` | mission records |
| GET | `/api/mission/active?trace=true` | loaded mission, plan and trace |
| POST | `/api/mission/load` · `/unload` · `/start` · `/pause` · `/resume` · `/stop` · `/acknowledge` | lifecycle |
| GET | `/api/sensors` · `/api/localization` | sensor and localization status |
| GET/PATCH | `/api/detections` · `/api/detections/{id}` | list / set review status |
| GET/POST/PUT/DELETE | `/api/boundaries[/{id}]` | saved GPS boundaries |
| GET | `/api/camera/status` · `/snapshot` · `/stream` | camera |
| GET | `/api/vision/status` | detector status and latest result |
| GET/POST | `/api/sim` · `/sim/speed` · `/sim/faults` · `/sim/pose` | simulation only (409 otherwise) |

### Grid plan request

```json
{"units": "m", "length": 4, "width": 2, "lane_spacing": 0.4, "swath_width": null, "margin": 0.1,
 "start_corner": "near_left", "lane_orientation": "along_length", "initial_heading_deg": null,
 "turn_strategy": "pivot", "turn_radius": null, "speed_mps": 0.2, "execution_mode": "simulation"}
```

### GPS plan request

```json
{"boundary": [{"lat": 12.97, "lon": 77.59}, ...], "units": "m", "lane_spacing": 1.0, "margin": 0.3,
 "lane_heading_deg": null, "start_vertex": 0, "turn_strategy": "pivot", "speed_mps": 0.2,
 "execution_mode": "hardware_gps"}
```

`execution_mode` must equal the running mode; it is an explicit confirmation,
not a switch.

## WebSockets

### `/ws/telemetry`

The server sends `{"type": "hello", "system": {...}, "events": [...]}`, then
`{"type": "telemetry", ...}` at `server.telemetry_rate_hz` (default 5 Hz), and
`{"type": "event", "event": {...}}` as events occur.

### `/ws/control` (manual driving)

```
→ {"type":"acquire","client_id":"tablet-1","takeover":false}
← {"type":"acquired","session_id":"…","heartbeat_interval_s":0.1,"deadman_timeout_s":0.35}
→ {"type":"drive","seq":1,"mode":"direction","direction":"forward","speed":0.5}
→ {"type":"drive","seq":2,"mode":"vector","throttle":0.6,"steer":-0.2,"speed":0.7}
← {"type":"ack","seq":2,"accepted":true,"left":0.56,"right":0.7}
→ {"type":"stop","seq":3}
→ {"type":"estop"}
→ {"type":"release"}
← {"type":"error","code":"stale_command|stale_session|motion_not_permitted|hold_limit|mission_active|session_busy|invalid_command","message":"…"}
```

Directions: `forward reverse left right forward_left forward_right reverse_left reverse_right stop`.
Re-send the held command every `heartbeat_interval_s`. Sequence numbers must
strictly increase within a session. Closing the socket stops the rover
immediately.
