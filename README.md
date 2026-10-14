# Autonomous Survey Rover Platform

Control, planning, safety and telemetry software for a four-wheel-drive survey
rover built on a **Raspberry Pi 5**, an **L298N** motor driver, an **HC-SR04**
ultrasonic sensor, an **IR obstacle sensor** and an **ESP32-CAM**. It runs on the
Pi and is operated from a browser on a laptop or tablet on the same Wi-Fi.

> **Scope.** This is a survey-rover *prototype* for controlled testing, sensor
> evaluation, mapping and logging. It does not handle, neutralise or interact
> with explosives. Obstacle sensors are not landmine detectors, and camera object
> detection (YOLOv8n) is not a mine detector. Detection events are unverified
> candidate signals that require human review.

## What it does

| Area | Summary |
| --- | --- |
| Manual control | Tank steering with buttons, diagonal moves, joystick and keyboard; speed limit; server-side dead-man; continuous-drive limit; reversal interlock |
| Safety | Starts disarmed; explicit arming with preflight checks; latched e-stop with explicit reset; single motion owner; command leases; thread watchdog on the motor driver; obstacle gating; fault registry |
| Grid planning (no GPS) | Boustrophedon coverage of a rectangle in m / cm / mm, margins, start corner, lane orientation, pivot or arc turns, preview, coverage and duration estimates |
| GPS planning | Draw / edit / save polygons on a Leaflet map; planning in a local ENU metric frame; concave shapes split into cells; Dubins arc turns; boundary checks |
| Missions | Explicit state machine (IDLE → READY → ARMING → RUNNING ⇄ PAUSED → STOPPING / COMPLETED / FAILED / EMERGENCY_STOP); persistence; interrupted-mission recovery without auto-resume |
| Localization | None (honest open-loop), simulated truth, simulated GPS, real NMEA GPS, experimental wheel-encoder odometry; magnetometer heading assessment |
| Sensors | Standard `Sensor` interface with structured readings; every device optional; reconnection; staleness; capability registry |
| Camera | ESP32-CAM firmware; the backend is the single stream client and re-serves MJPEG to all browsers; YOLOv8n via ONNX Runtime (optional) |
| Simulation | Full simulated rover with deliberate open-loop drift, obstacles, metal targets, magnetic field, fault injection, adjustable speed |

## Repository layout

```
backend/    Python 3.11+ FastAPI service (runs on the Pi)
  rover/    application package (see docs/ARCHITECTURE.md)
  config/   simulation.yaml, simulation_gps.yaml, minimal_simulation.yaml, hardware.yaml, hardware_gps.yaml
  tests/    pytest suite (124 tests)
frontend/   React + TypeScript + Vite + Tailwind web UI
firmware/   ESP32-CAM Arduino sketch
deploy/     Raspberry Pi install script and systemd unit
docs/       architecture, safety, hardware, sensors, coordinates, calibration, camera/vision, API, operations
```

## Quick start: simulation on a laptop

Requirements: Python 3.11+, Node.js 18+.

```bash
# backend
cd backend
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m rover --config config/simulation.yaml        # http://localhost:8000

# frontend (second terminal)
cd frontend
npm install
npm run dev                     # http://localhost:5173 (proxies /api and /ws to :8000)
```

Or build the UI once (`npm run build`). The backend then serves it at
`http://localhost:8000`.

Try it out:

1. **Dashboard**: the violet `SIM` badge marks simulation; every simulated device is labelled.
2. **Drive**: arm (simulated), take manual control, then use the arrows/WASD or the joystick.
   Release a control and the rover stops. Close the tab and the server's dead-man stops it.
3. **Grid plan**: plan a 4 × 2 m survey, then *Save & load*. On **Mission**, arm and start.
   Without localization it runs open-loop. The amber outline is the *commanded estimate*,
   and the violet glyph is the simulator's ground truth. They drift apart on purpose.
4. **System**: speed up the simulation, inject faults (sensor dropout, GPS loss, link drop,
   control-loop stall) and watch the safety responses.
5. `config/simulation_gps.yaml` exercises GPS planning and GPS-guided execution with a simulated receiver.

## Deploying on the Raspberry Pi 5

```bash
git clone <this repo> ~/rover && cd ~/rover
./deploy/install_pi.sh --config config/hardware.yaml          # add --vision for YOLO, --build-frontend if Node is installed
```

This installs `python3-lgpio`, creates `backend/.venv` with `--system-site-packages`,
validates the config, and enables the `rover-backend` systemd service. Open
`http://<pi-ip>:8000` from a laptop or tablet. Read **docs/HARDWARE.md** (wiring,
level shifting, pull-downs) and **docs/SAFETY.md** before powering the motors.

The frontend is a separate static build. It can also be hosted elsewhere and
pointed at the Pi from **Connection** settings or with `VITE_BACKEND_URL`.

## Operating modes

| Mode | Config | Motors | Position | Notes |
| --- | --- | --- | --- | --- |
| Simulation | `simulation.yaml` | simulated | none / simulated / simulated GPS | No GPIO. Faults injectable. |
| Real hardware, no GPS | `hardware.yaml` | L298N | none | Grid missions run **open-loop**; progress is commanded, not measured |
| Real hardware + GPS | `hardware_gps.yaml` | L298N | NMEA GNSS | GPS missions need a valid fix; heading from course-over-ground only while moving |

The mode comes only from the configuration file. The process never switches
between simulation and hardware, and configuration validation rejects simulated
devices in hardware modes. A real device that fails to initialise is reported as
unavailable; it is **never** replaced with a simulated one.

## Tests

```bash
cd backend && python -m pytest          # 124 tests: planner, safety, motion, manual dead-man, missions,
                                         # GNSS/NMEA, sensors & optional hardware, capabilities, API/WebSocket, vision
cd frontend && npm test && npm run build
```

## Honest status of hardware support

Implemented, but **not yet verified on the physical rover**: the L298N driver,
HC-SR04, GPIO IR, digital metal-detector input, NMEA GPS, the physical e-stop
input, the Linux thermal sensor and the ESP32-CAM ingest. Interfaces only
(adapters to be written when hardware is chosen): magnetometer, IMU, wheel
encoders and battery monitor. See **docs/SENSORS.md** for the full matrix and the
verification procedure.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, control authority, state machine, threading, simulation time
- [docs/SAFETY.md](docs/SAFETY.md): safety design, limits of a software e-stop, required hardware measures
- [docs/HARDWARE.md](docs/HARDWARE.md): wiring, pin map, electrical notes
- [docs/SENSORS.md](docs/SENSORS.md): sensor framework, optional hardware, capabilities, adding drivers, verification
- [docs/COORDINATES.md](docs/COORDINATES.md): frames, headings, grid and ENU conventions
- [docs/CALIBRATION.md](docs/CALIBRATION.md): measuring the open-loop motion model
- [docs/CAMERA_VISION.md](docs/CAMERA_VISION.md): ESP32-CAM, YOLOv8n export, limitations
- [docs/API.md](docs/API.md): REST and WebSocket reference
- [docs/OPERATIONS.md](docs/OPERATIONS.md): field checklist
- [docs/TESTING.md](docs/TESTING.md): how to run tests; requirement-to-test map
