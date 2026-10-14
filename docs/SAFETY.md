# Safety

Safety is a subsystem, not a UI feature. Every rule below is enforced in the
backend; the frontend only displays state and requests actions.

## What the software does

| Mechanism | Behaviour |
| --- | --- |
| Safe start | Process starts **disarmed** with zero output. The e-stop latch is clear, but nothing can move until an operator arms. |
| Arming | `POST /api/safety/arm` runs preflight checks: motor driver initialised, control loop ticking, e-stop latch clear, physical e-stop released (if fitted), no ERROR/CRITICAL faults, every `required_for_arming` sensor connected with fresh valid data. Arming is refused with the list of failed checks. |
| Auto-disarm | Disarms after `safety.auto_disarm_idle_s` without motion (not while a mission owns the motors). |
| Single owner | Only one of {manual session, mission} can command motion. |
| Command leases | Each command expires (manual: `manual.deadman_timeout_s`, default 0.35 s; mission: 0.4 s). An expired lease means zero output. |
| Dead-man | The browser re-sends drive commands every 100 ms while a control is held. Releasing sends a stop. A closed socket immediately closes the session and zeroes outputs. Stale or replayed sequence numbers and old sessions are rejected. |
| Hold limit | Continuous manual driving is limited to `manual.max_continuous_s`; the operator must release and press again. |
| Limits | `motion.max_output`, accel/decel ramps, reversal interlock with dwell. Invalid values (NaN, ±inf, > 1) are rejected, not clamped. |
| Obstacle gating | Forward motion is blocked when a front sensor sees an obstacle within `obstacle_stop_distance_m`, and also when a front sensor is unavailable, stale or invalid (`block_forward_on_sensor_invalid`). Missing data is never treated as a clear path. Reverse and pivots remain possible so the operator can back away. |
| Missions | Obstacle → pause (or fail with `obstacle_policy: stop`). Localization lost → pause after `loss_pause_s`, fail after `loss_abort_s`. Resume requires the operator, valid localization and a clear path. Disarm → pause. |
| E-stop | Immediate zero output (no ramp), lease dropped, command epoch incremented, rover disarmed, mission → `EMERGENCY_STOP` and recorded as `emergency_stopped`. The latch persists until an explicit reset; after reset the rover stays **disarmed** and the mission is never resumed. |
| Driver watchdog | An OS thread stops the motors if the control loop has not updated the driver within `motors.watchdog_timeout_s` (default 0.5 s). |
| Faults | Faults are registered with severity: ERROR blocks arming and CRITICAL blocks motion (e.g. motor driver errors, a crashed control task). They are persisted with raise/clear times. |
| Restart | Missions active at crash or reboot are marked `interrupted` and need operator review. They never auto-resume. |
| Hardware honesty | A real device that fails to initialise is reported unavailable. It is never replaced with a simulated device. |

## What the software cannot do

* **A software e-stop is not an independent safety circuit.** It depends on the Pi,
  the OS, Python, the network and the GPIO lines all working. The UI says so.
* No wheel-speed feedback exists. The software knows what it *commanded*, not
  what the wheels did.
* Open-loop missions cannot verify where the rover is. A stalled or slipping
  rover will be reported as progressing.
* The HC-SR04 has a ~15° beam and misses thin, soft or angled objects. The IR
  module sees only a short range. Neither sees drops or holes.

## Required hardware measures for real operation

1. **Physical motor-power cut-off.** A latching mushroom e-stop switch in series with
   the motor battery supply to the L298N, within the operator's reach. It must
   work with the Pi crashed.
2. **Pull-down resistors** (10 kΩ to GND) on L298N IN1–IN4 and ENA/ENB. When the Pi
   boots, crashes or releases its GPIO lines, the inputs float; pull-downs disable
   the bridge.
3. Optionally wire the e-stop's auxiliary contact to a GPIO
   (`safety.estop_input`) so the software also latches. Read failures count as
   "pressed".
4. **Level shifting** for the HC-SR04 Echo line (5 V → 3.3 V). See HARDWARE.md.
5. Separate battery or regulator for the Pi, isolated from motor current spikes.
6. Start with `motion.max_output: 0.5–0.7`, test in an enclosed area, and keep
   people out of the survey area during autonomous runs.

## Testing the safety behaviour in simulation

**System → Simulation controls → Fault injection**:

| Fault | Expected result |
| --- | --- |
| `ultrasonic_no_echo` | Front state *invalid*; forward motion blocked; a running mission pauses |
| `ultrasonic_stale` | Front state *stale* after `stale_after_s`; forward blocked |
| `ultrasonic_disconnect` | Sensor *disconnected*; ERROR fault; arming refused; auto-reconnect when cleared |
| `control_link_drop` | Manual commands ignored; the dead-man stops the rover within 0.35 s |
| `control_loop_stall` | Driver watchdog trips; motors forced to zero; event logged |
| `gps_loss` (GPS sim) | No position reported; GPS mission pauses; fails after `loss_abort_s` |
| `motor_driver_fault` | Simulated driver reports unhealthy |

The automated tests (`backend/tests/test_motion.py`, `test_mission.py`,
`test_api.py`, `test_sensors.py`) check all of these.
