# Sensors, optional hardware and capabilities

The rover is a platform, not a fixed set of devices. Every sensor is an
independent, optional entry in the `sensors:` list of the configuration. A
missing, disabled, failing or not-yet-purchased device never stops the
application. It only disables the capabilities that depend on it.

## Support matrix

| Device | Type key | Drivers | Status |
| --- | --- | --- | --- |
| HC-SR04 ultrasonic (any number) | `ultrasonic` | `hcsr04`, `simulated`, `external` | Implemented; **not yet verified on the physical rover** |
| IR obstacle module (any number) | `ir_obstacle` | `gpio`, `simulated`, `external` | Implemented; not yet physically verified |
| Metal detector | `metal_detector` | `gpio_digital` (comparator output), `simulated`, `external` | Digital input implemented, not physically verified; analogue detectors need an `external` adapter |
| Magnetometer | `magnetometer` | `simulated`, `external` | **Interface + simulation only.** No model assumed; write an adapter when hardware is chosen |
| IMU | `imu` | `simulated`, `external` | Interface + simulation only |
| Wheel encoders (left/right) | `wheel_encoder` | `simulated`, `external` | Interface + simulation only |
| Battery voltage/current | `battery` | `simulated`, `external` | Interface + simulation only |
| Temperature | `temperature` | `linux_thermal` (Pi SoC), `simulated`, `external` | `linux_thermal` implemented, not yet verified on a Pi 5 |
| GNSS receiver | `localization.provider: gps_nmea` | NMEA over serial | Implemented and unit-tested with recorded sentences; not yet field-tested |
| ESP32-CAM | `camera.provider: esp32cam` | MJPEG over HTTP | Firmware and ingest implemented; not yet tested with the physical module |
| Camera detection | `vision` | YOLOv8n via ONNX Runtime / Ultralytics | Pipeline tested with a synthetic model; stock weights detect COCO objects only |

"Implemented, not verified" means the code follows the device's documented
behaviour and passes automated tests, but has not yet run against the physical
part. Drivers declare `physically_verified = False`, and the UI shows an
*unverified driver* badge.

## Capabilities and their dependencies

| Capability | Depends on | Without it |
| --- | --- | --- |
| `manual_control` | motor driver, control loop | — (core) |
| `obstacle_sensing` | ultrasonic/IR sensors with `safety_role` | *not installed* if none configured (motion unprotected, UI warns); *unavailable* if all are failing (forward blocked) |
| `grid_planning` | nothing | always ready |
| `grid_execution` | manual_control (+ local pose for closed loop) | open-loop, labelled *degraded* on hardware |
| `gps_planning` | nothing | always ready (planning works without a receiver) |
| `gps_navigation` | GNSS provider with a valid fix | GPS missions can be planned and saved, but not started |
| `encoder_odometry` | left + right `wheel_encoder` | not installed |
| `heading_estimation` | provider heading (sim / odometry / GNSS course-over-ground) | unavailable; magnetometer heading is diagnostic only |
| `magnetometer`, `imu`, `battery_monitoring`, `temperature_monitoring` | the respective sensor | not installed / unavailable |
| `detection_logging` | metal detector | not installed; events never generated |
| `camera_streaming`, `object_detection` | camera; vision enabled + model file | not installed / unavailable |
| `localization` | any position provider | not installed → open-loop only |

Status values: `ready` (real hardware), `simulated` (works, simulation only),
`degraded`, `unavailable` (configured but failing or invalid), `not_installed`.
Capabilities are evaluated from **live** device state, not from configuration
alone. Each is a separate function in `rover/capabilities.py`, registered with
`@capability("id")`.

## Reading schema

```json
{
  "sensor_id": "magnetometer",
  "sensor_type": "magnetometer",
  "timestamp": "2026-10-14T15:31:02.481+00:00",
  "value": {"x": -13.2, "y": 30.4, "z": -25.9, "magnitude": 42.1},
  "units": {"x": "uT", "y": "uT", "z": "uT", "magnitude": "uT"},
  "valid": true,
  "simulated": true,
  "error": null,
  "metadata": {"frame": "body_xright_yforward_zup"}
}
```

Each sensor also reports a **connection state** (`disabled`, `configured`,
`initializing`, `connected`, `disconnected`, `faulty`) separately from its
**reading health** (`ok`, `invalid`, `stale`, `none`), plus calibration
metadata, error text, reconnect count and age of the last reading.

## Magnetometer

The magnetometer is reserved and simulated, but no model is assumed. Raw
three-axis field readings are always shown. A magnetic **heading** is reported
only if all of these hold:

* hard/soft-iron calibration is recorded (`calibration.calibrated: true`);
* tilt compensation is available (requires IMU orientation fusion, **not
  implemented**) **or** the operator asserts level operation (`assume_level: true`);
* the corrected field magnitude is within `expected_field_ut` (otherwise interference
  from the motors, chassis or wiring is likely);
* motor interference has been characterised, or the motors are idle.

Otherwise the heading is shown as *unavailable/unreliable* with the reasons. It
is never used for navigation.

## Localization and fusion: what exists

| Method | Status |
| --- | --- |
| No localization (honest open-loop) | Implemented |
| Simulated ground truth | Implemented (simulation only) |
| GNSS position + course-over-ground heading (moving only) | Implemented |
| Wheel-odometry dead reckoning (`provider: odometry`) | Implemented, **experimental**, tested with simulated encoders only |
| Odometry with IMU yaw rate | Implemented, experimental, simulation-tested |
| GNSS + odometry fusion, IMU orientation, magnetometer fusion | **Not implemented** (interfaces exist) |

## Configuring and disabling devices

```yaml
sensors:
  - id: rear_ultrasonic           # second ultrasonic guarding reverse motion
    type: ultrasonic
    driver: hcsr04
    trigger_pin: 20
    echo_pin: 21
    safety_role: rear
    mount: {x_m: 0, y_m: -0.12, yaw_deg: 180}
  - id: magnetometer
    type: magnetometer
    enabled: false                 # not fitted yet
```

* `enabled: false` keeps the device listed (UI: "configured but disabled").
* `safety.required_for_arming` lists sensor IDs that must be healthy before arming.
* Validation runs at startup and in `python -m rover --config X --check-config`.
  Typos, duplicate IDs, duplicate GPIO pins and simulated drivers in hardware
  mode are rejected with clear messages.

## Adding a new sensor driver

1. **Config** (only for a new device *type*): add a `SensorBaseConfig` subclass with
   `type: Literal["my_type"]` in `rover/config.py` and add it to the `SensorConfig` union.
2. **Driver**: subclass `rover.sensors.base.Sensor`:

```python
from rover.sensors.base import Sensor, SensorError, SensorInitError
from rover.sensors.drivers import register

@register
class Qmc5883lMagnetometer(Sensor):
    sensor_type = "magnetometer"
    driver_name = "qmc5883l"
    simulated = False
    physically_verified = False   # set True only after bench verification

    def initialize(self) -> None:
        try:
            ...  # open I2C bus self.config.i2c_bus at self.config.i2c_address
        except OSError as exc:
            raise SensorInitError(str(exc)) from exc

    def acquire(self):
        try:
            x, y, z = ...  # read registers, convert to microtesla
        except OSError as exc:
            raise SensorError(str(exc)) from exc  # counts towards disconnection
        return self.reading({"x": x, "y": y, "z": z}, {"x": "uT", "y": "uT", "z": "uT"})

    def close(self) -> None:
        ...
```

   Either add the driver name to the config `Literal`, or skip registration and
   load it without code changes using `driver: external` and
   `adapter: "my_package.my_module:Qmc5883lMagnetometer"`.
3. **Capability** (optional): add a `@capability("...")` function.
4. **Frontend** (optional): the Sensors page renders any reading generically. Add a
   case in `SensorCard` for a custom widget.

Motion control, planning and the mission manager need no changes. The test
`tests/test_sensors.py::test_external_adapter_plugs_in` shows an external
adapter being loaded.

## Testing a sensor in simulation

* Enable the simulated variant in `config/simulation.yaml` (`driver: simulated`).
* Inject faults from **System → Simulation controls** (`magnetometer_disconnect`,
  `ultrasonic_stale`, …) and watch the state, health and capability changes.
* Automated: `pytest tests/test_sensors.py`.

## Verifying real readings after installation

1. Add the sensor with `enabled: true` and run `--check-config`.
2. Start the backend and open **Sensors**. Expect `connected` and a `valid`
   badge; anything else shows the driver's error text.
3. Compare against a reference: tape measure for ultrasonic, a hand at known
   distances for IR, a phone compass away from the rover for a magnetometer,
   a multimeter for battery voltage.
4. Check failure handling: disconnect the device. Expect `disconnected` and a
   fault, with only the dependent capabilities going unavailable. Reconnect it
   and expect automatic recovery.
5. Only then set `physically_verified = True` in the driver class and record the
   test in your lab notes.
