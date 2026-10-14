# Architecture

## Components

```
 Browser (React)                         Raspberry Pi 5 — rover backend (FastAPI, one process)
 ─────────────────                       ────────────────────────────────────────────────────────────────
 REST  /api/*  ───────────────►  api/routes.py ──► MissionManager ──► MissionExecutor ─┐
 WS    /ws/telemetry ◄───────── api/ws.py ◄── RoverRuntime.telemetry()                 │ leases
 WS    /ws/control   ─────────► ManualControlService ──────────────────────────────────┤
                                                                                       ▼
                                 SafetySupervisor ──permit/obstacles──► MotionController ──► WatchdogMotorDriver ──► L298N / SimMotorDriver
                                        ▲                                                         (OS thread watchdog)
                                        │ assessment
                                 SensorManager ──► Sensor drivers (HC-SR04, IR, metal, magnetometer, IMU, encoders, battery, temperature, external adapters)
                                 LocalizationService ──► provider (none | simulated | simulated_gps | gps_nmea | odometry)
                                 CameraProvider (ESP32-CAM MJPEG / simulated) ──► VisionService (YOLOv8n ONNX) ──► DetectionRecorder
                                 Repository (SQLAlchemy + SQLite, versioned migrations)
                                 SimWorld (simulation only)
```

`rover/runtime.py` is the composition root. It builds every subsystem from the
validated configuration, starts the background tasks, and builds telemetry
snapshots and capability reports.

## Control authority

* **`MotionController` is the only code that writes motor outputs.** Web routes,
  WebSocket handlers, the database and callbacks never touch GPIO.
* One **owner** at a time: `manual` (a WebSocket session) or `mission` (the
  executor). `acquire` fails if someone else owns the controller, so manual
  driving during a mission is impossible, and vice versa.
* Every command is a **lease** with a short real-time TTL (manual: dead-man
  timeout; mission: `motion.mission_command_timeout_s`). The 50 Hz control loop
  outputs zero when the lease lapses.
* Before reaching the driver, outputs are clamped to `max_output`, rate-limited
  (accel/decel), passed through the reversal interlock (ramp to 0, dwell, then
  reverse), and gated by the obstacle assessment (forward or reverse blocked).
* `emergency_halt` zeroes outputs immediately, drops the lease and increments the
  command **epoch**, so a lease issued before an e-stop can never take effect.
* `WatchdogMotorDriver` runs an OS thread that stops the motors if the control
  loop stops updating them (asyncio hang, crash), independently of the event loop.

## Mission state machine (`mission/state_machine.py`)

```
IDLE ──load──► READY ──start──► ARMING ──checks ok + countdown──► RUNNING ◄──resume── PAUSED
  ▲              │  ▲             │                                 │   └──pause──►   │
  │           unload └─checks fail┘                                 │                  │
  │                                                     complete ───┼──► COMPLETED ──ack──► IDLE
  │                                                     abort ──────┼──► FAILED ─────ack──► IDLE
  │                                                     stop ───────┴──► STOPPING ─────────► IDLE
  └──────────── e-stop reset ◄── EMERGENCY_STOP ◄── (any state)
```

Every transition outside the table raises `InvalidTransition` (HTTP 409). Every
transition is logged and persisted in `mission_events`. `EMERGENCY_STOP` only
leads back to `IDLE`, and the interrupted mission is marked
`emergency_stopped` and can never be reloaded.

Only the `MissionManager` runs missions. It creates exactly one
`MissionExecutor` task. The executor never changes lifecycle state itself: it
asks the manager to pause, or raises `MissionAbort`.

### Execution modes

| Mode | When | Position source | Turns |
| --- | --- | --- | --- |
| `open_loop` | grid mission, no local pose provider | none — integrates the *applied* outputs through the calibration model (commanded estimate) | timed pivots/arcs |
| `closed_loop` | grid mission + `simulated` or `odometry` provider | local pose with heading | pure pursuit, closed-loop pivots |
| `gnss` | GPS mission | GNSS fix converted into the plan's ENU frame | open-loop pivots (no stationary heading); creep straight to acquire course-over-ground |

## Sensors, localization and capabilities

* `sensors/base.py` defines `Sensor` and `SensorReading` (structured `value` +
  `units`, timestamp, validity, `simulated`, metadata).
* `sensors/manager.py` runs one task per enabled sensor: initialise (with
  timeout), sample at `rate_hz` in a worker thread, detect stale and invalid
  data, declare disconnection after N errors, and retry with backoff. Failures
  raise WARNING faults (ERROR for sensors in `safety.required_for_arming`).
* `localization/` keeps raw acquisition (NMEA parsing, encoder readings) separate
  from estimation (`GnssState`, `OdometryProvider`) and from navigation (executor).
* `capabilities.py` holds independently registered capability checks that
  inspect live state, so the UI can explain what is ready, simulated, degraded,
  unavailable or not installed.

## Threading and timing

* Single asyncio event loop: control loop (50 Hz), safety supervisor (25 Hz),
  sensor tasks, executor (20 Hz), telemetry, persistence.
* Blocking work runs in worker threads: sensor reads (HC-SR04 echo timing), YOLO
  inference, SQLite telemetry writes. The GPS reader and motor watchdog are
  dedicated OS threads.
* **Two clocks.** Safety timing (leases, dead-man, staleness, watchdogs) always
  uses real monotonic time. Physics, executor timing and ramps use the *mission
  clock*. In simulation the mission clock can run up to 20× faster, and the
  control, executor and simulated-sensor loops speed up with it so control
  resolution in simulated time is unchanged. On hardware both clocks are equal.

## Persistence

SQLite (WAL mode) via SQLAlchemy 2. Schema versions are tracked in
`schema_version`, and migrations are an ordered list in `persistence/db.py`
(append new ones; never edit released ones). A database newer than the software
is refused. Tables: missions, mission_events, event_log, fault_events,
telemetry_samples (1 Hz while a mission is active), sensor_samples,
detection_events, survey_boundaries, config_metadata. Old telemetry is pruned
after `database.retention_days`.

On startup, missions still marked `active` (crash or power loss) become
`interrupted`, are flagged for operator review, and are never resumed.

## Shutdown

`SIGINT`/`SIGTERM` (systemd uses `KillSignal=SIGINT`) runs the FastAPI lifespan
shutdown: emergency halt first, then stop any mission, cancel tasks, close
sensors, drivers and GPIO, and close the database. An `atexit` hook and the
driver watchdog are further fallbacks. If the process is killed with SIGKILL,
the GPIO lines are released and the L298N inputs must be held low by **hardware
pull-down resistors** (see HARDWARE.md).
