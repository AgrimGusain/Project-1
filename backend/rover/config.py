"""Validated configuration schema.

Configuration is loaded from a YAML file (``ROVER_CONFIG`` env var or ``--config``)
and validated with Pydantic. Unknown keys are rejected so that typos in safety
parameters cannot silently fall back to defaults.

The operating mode is **only** taken from the configuration file. The process
never switches between simulation and real hardware on its own, and the
validators below refuse configurations that would mix simulated devices into a
real-hardware mode (or real GPIO devices into simulation).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rover.core.enums import OperatingMode


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- server


class ServerConfig(_Base):
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    # Optional: serve the built frontend from the backend for convenience.
    frontend_dist: str | None = "../frontend/dist"
    telemetry_rate_hz: float = Field(5.0, gt=0, le=30)


class AuthConfig(_Base):
    enabled: bool = False
    # Prefer the ROVER_AUTH_TOKEN environment variable over storing it in YAML.
    token: str | None = None


class DatabaseConfig(_Base):
    path: str = "data/rover.db"
    telemetry_sample_hz: float = Field(1.0, gt=0, le=10)
    record_idle_telemetry: bool = False
    retention_days: int = Field(60, ge=1)


# --------------------------------------------------------------------------- hardware


class GpioConfig(_Base):
    # "auto" finds the RP1 header chip on a Pi 5 (gpiochip0 on recent kernels,
    # gpiochip4 on early Bookworm kernels).
    chip: int | Literal["auto"] = "auto"


class MotorSideConfig(_Base):
    in_a: int = Field(..., ge=0, le=27, description="L298N IN1/IN3 (BCM)")
    in_b: int = Field(..., ge=0, le=27, description="L298N IN2/IN4 (BCM)")
    enable: int = Field(..., ge=0, le=27, description="L298N ENA/ENB PWM pin (BCM)")
    invert: bool = False


class MotorConfig(_Base):
    driver: Literal["l298n", "simulated"] = "simulated"
    left: MotorSideConfig = MotorSideConfig(in_a=17, in_b=27, enable=12)
    right: MotorSideConfig = MotorSideConfig(in_a=22, in_b=23, enable=13)
    pwm_frequency_hz: int = Field(1000, ge=50, le=10000)
    # Duty cycle below which the gearmotors stall; non-zero commands are mapped
    # into [min_duty, 1.0].
    min_duty: float = Field(0.30, ge=0.0, le=0.9)
    # Independent driver watchdog: if no output update arrives for this long the
    # driver thread forces the outputs to zero, even if the asyncio loop hangs.
    watchdog_timeout_s: float = Field(0.5, gt=0.05, le=5.0)


class MotionConfig(_Base):
    control_rate_hz: float = Field(50.0, ge=10, le=200)
    max_output: float = Field(0.85, gt=0, le=1.0)
    max_accel_per_s: float = Field(2.5, gt=0)
    max_decel_per_s: float = Field(5.0, gt=0)
    reversal_dwell_s: float = Field(0.25, ge=0)
    mission_command_timeout_s: float = Field(0.4, gt=0.05, le=3.0)
    obstacle_slow_output: float = Field(0.35, gt=0, le=1.0)


class CalibrationConfig(_Base):
    """Open-loop differential-drive model. These numbers MUST be measured on the real rover.

    Ground speed of each side = output * full_speed_mps. Yaw rate (rad/s) =
    (v_left - v_right) / effective_track_m. A skid-steer chassis scrubs while
    turning, so the effective track is larger than the physical one; measure it
    from a timed pivot: effective_track_m = 2 * full_speed_mps / pivot_rate_rad_s.
    """

    full_speed_mps: float = Field(0.45, gt=0, description="Ground speed at output 1.0")
    effective_track_m: float = Field(0.37, gt=0, description="Effective (skid) track width")
    calibrated: bool = False
    notes: str = ""

    @property
    def full_pivot_dps(self) -> float:
        import math

        return math.degrees(2.0 * self.full_speed_mps / self.effective_track_m)


class ManualConfig(_Base):
    deadman_timeout_s: float = Field(0.35, ge=0.1, le=2.0)
    heartbeat_interval_s: float = Field(0.1, ge=0.03, le=0.5)
    max_continuous_s: float = Field(20.0, ge=1.0, le=600.0)
    default_speed: float = Field(0.5, gt=0, le=1.0)
    max_speed: float = Field(0.8, gt=0, le=1.0)
    turn_ratio: float = Field(0.5, ge=0, le=1.0, description="Inner-wheel ratio for diagonal moves")
    block_forward_on_obstacle: bool = True


class EstopInputConfig(_Base):
    enabled: bool = False
    pin: int = Field(26, ge=0, le=27)
    active_low: bool = True
    pull: Literal["up", "down", "none"] = "up"


class SafetyConfig(_Base):
    obstacle_stop_distance_m: float = Field(0.30, gt=0)
    obstacle_slow_distance_m: float = Field(0.60, gt=0)
    obstacle_policy: Literal["pause", "stop"] = "pause"
    # Missing/stale/invalid range data is never treated as a clear path.
    block_forward_on_sensor_invalid: bool = True
    sensor_stale_s: float = Field(0.6, gt=0)
    # Sensor ids that must be connected and producing valid data before arming.
    required_for_arming: list[str] = Field(default_factory=lambda: ["front_ultrasonic"])
    auto_disarm_idle_s: float = Field(300.0, ge=0, description="0 disables")
    estop_input: EstopInputConfig = EstopInputConfig()

    @model_validator(mode="after")
    def _check(self) -> "SafetyConfig":
        if self.obstacle_slow_distance_m < self.obstacle_stop_distance_m:
            raise ValueError("obstacle_slow_distance_m must be >= obstacle_stop_distance_m")
        return self


# --------------------------------------------------------------------------- sensors
#
# Every sensor is an independent, optional device with its own section in the
# ``sensors`` list. The ``type`` field selects the device schema (discriminated
# union) and ``driver`` selects the implementation. Adding a new device type means
# adding one config class here plus a driver registered in ``rover.sensors.drivers``;
# nothing in motion control, planning or the mission manager changes.

SENSOR_ID_PATTERN = r"^[a-z][a-z0-9_]{1,40}$"
# Drivers that touch Raspberry Pi GPIO lines. Never allowed in simulation mode.
GPIO_DRIVERS = {"hcsr04", "gpio", "gpio_digital"}


class SensorMount(_Base):
    """Mounting position in the rover body frame (x right, y forward, metres)."""

    x_m: float = 0.0
    y_m: float = 0.10
    yaw_deg: float = Field(0.0, description="Sensor boresight relative to rover heading (cw +)")


class SensorBaseConfig(_Base):
    id: str = Field(..., pattern=SENSOR_ID_PATTERN)
    label: str | None = None
    enabled: bool = True
    rate_hz: float = Field(10.0, gt=0, le=200)
    stale_after_s: float | None = Field(None, gt=0, description="Default: max(3/rate_hz, 0.3)")
    read_timeout_s: float = Field(0.5, gt=0, le=10)
    reconnect_interval_s: float = Field(3.0, gt=0, le=300)
    # Consecutive read errors before the device is declared disconnected.
    max_consecutive_errors: int = Field(5, ge=1, le=1000)
    # For driver="external": "package.module:ClassName" implementing rover.sensors.base.Sensor.
    adapter: str | None = Field(None, pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
    adapter_options: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_stale_s(self) -> float:
        return self.stale_after_s if self.stale_after_s is not None else max(3.0 / self.rate_hz, 0.3)

    @model_validator(mode="after")
    def _adapter(self) -> "SensorBaseConfig":
        driver = getattr(self, "driver", None)
        if driver == "external" and not self.adapter:
            raise ValueError(f"sensor '{self.id}': driver=external requires 'adapter: module:Class'")
        return self


class UltrasonicSensorConfig(SensorBaseConfig):
    type: Literal["ultrasonic"] = "ultrasonic"
    driver: Literal["hcsr04", "simulated", "external"] = "simulated"
    rate_hz: float = Field(12.0, gt=0, le=25)
    trigger_pin: int | None = Field(None, ge=0, le=27)
    echo_pin: int | None = Field(None, ge=0, le=27)
    min_range_m: float = Field(0.02, ge=0)
    max_range_m: float = Field(4.0, gt=0, le=6.0)
    temperature_c: float = 20.0
    beam_half_angle_deg: float = Field(15.0, gt=0, le=45)
    mount: SensorMount = SensorMount()
    # Which motion direction this sensor guards. "none" = informational only.
    safety_role: Literal["front", "rear", "none"] = "front"

    @model_validator(mode="after")
    def _pins(self) -> "UltrasonicSensorConfig":
        if self.driver == "hcsr04" and (self.trigger_pin is None or self.echo_pin is None):
            raise ValueError(f"sensor '{self.id}': hcsr04 requires trigger_pin and echo_pin")
        if self.min_range_m >= self.max_range_m:
            raise ValueError(f"sensor '{self.id}': min_range_m must be < max_range_m")
        return self


class IrObstacleSensorConfig(SensorBaseConfig):
    type: Literal["ir_obstacle"] = "ir_obstacle"
    driver: Literal["gpio", "simulated", "external"] = "simulated"
    rate_hz: float = Field(30.0, gt=0, le=200)
    pin: int | None = Field(None, ge=0, le=27)
    # Most hobby IR modules pull their output LOW when an obstacle is seen.
    active_low: bool = True
    pull: Literal["up", "down", "none"] = "up"
    debounce_ms: float = Field(30.0, ge=0)
    nominal_range_m: float = Field(0.15, gt=0, description="Documentation / simulation only")
    mount: SensorMount = SensorMount()
    safety_role: Literal["front", "rear", "none"] = "front"

    @model_validator(mode="after")
    def _pin(self) -> "IrObstacleSensorConfig":
        if self.driver == "gpio" and self.pin is None:
            raise ValueError(f"sensor '{self.id}': gpio driver requires pin")
        return self


class MetalDetectorSensorConfig(SensorBaseConfig):
    type: Literal["metal_detector"] = "metal_detector"
    driver: Literal["simulated", "gpio_digital", "external"] = "simulated"
    pin: int | None = Field(None, ge=0, le=27)
    active_low: bool = False
    pull: Literal["up", "down", "none"] = "down"
    threshold: float = Field(0.5, ge=0)
    release_threshold: float = Field(0.35, ge=0)
    saturation: float = Field(1.0, gt=0)
    min_event_interval_s: float = Field(2.0, ge=0)
    calibration: dict[str, Any] = Field(default_factory=dict)
    mount: SensorMount = SensorMount(y_m=0.18)

    @model_validator(mode="after")
    def _check(self) -> "MetalDetectorSensorConfig":
        if self.driver == "gpio_digital" and self.pin is None:
            raise ValueError(f"sensor '{self.id}': gpio_digital driver requires pin")
        if self.release_threshold > self.threshold:
            raise ValueError(f"sensor '{self.id}': release_threshold must be <= threshold")
        return self


class MagnetometerCalibration(_Base):
    calibrated: bool = False
    method: str | None = None
    calibrated_at: str | None = None
    hard_iron_ut: tuple[float, float, float] = (0.0, 0.0, 0.0)
    soft_iron: tuple[
        tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]
    ] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    # Calibration should be repeated with motors running to capture their field.
    motor_interference_checked: bool = False


class MagnetometerSensorConfig(SensorBaseConfig):
    """No specific magnetometer model is assumed. Real devices plug in via driver=external."""

    type: Literal["magnetometer"] = "magnetometer"
    driver: Literal["simulated", "external"] = "simulated"
    rate_hz: float = Field(10.0, gt=0, le=200)
    i2c_bus: int | None = Field(None, ge=0)
    i2c_address: int | None = Field(None, ge=0, le=0x7F)
    calibration: MagnetometerCalibration = MagnetometerCalibration()
    declination_deg: float = Field(0.0, ge=-90, le=90)
    mount_yaw_deg: float = 0.0
    # Heading without tilt compensation is only acceptable if the operator asserts
    # the rover operates on level ground. Default: not assumed.
    assume_level: bool = False
    expected_field_ut: tuple[float, float] = (20.0, 70.0)


class ImuSensorConfig(SensorBaseConfig):
    type: Literal["imu"] = "imu"
    driver: Literal["simulated", "external"] = "simulated"
    rate_hz: float = Field(50.0, gt=0, le=200)
    i2c_bus: int | None = Field(None, ge=0)
    i2c_address: int | None = Field(None, ge=0, le=0x7F)
    gyro_bias_dps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    calibrated: bool = False


class WheelEncoderSensorConfig(SensorBaseConfig):
    type: Literal["wheel_encoder"] = "wheel_encoder"
    driver: Literal["simulated", "external"] = "simulated"
    rate_hz: float = Field(50.0, gt=0, le=200)
    side: Literal["left", "right"]
    counts_per_rev: float = Field(20.0, gt=0)
    wheel_diameter_m: float = Field(0.065, gt=0)
    pin_a: int | None = Field(None, ge=0, le=27)
    pin_b: int | None = Field(None, ge=0, le=27)
    calibrated: bool = False


class BatteryMonitorSensorConfig(SensorBaseConfig):
    type: Literal["battery"] = "battery"
    driver: Literal["simulated", "external"] = "simulated"
    rate_hz: float = Field(1.0, gt=0, le=50)
    stale_after_s: float | None = Field(5.0, gt=0)
    cells: int = Field(2, ge=1, le=12)
    min_cell_v: float = Field(3.4, gt=0)
    max_cell_v: float = Field(4.2, gt=0)
    warn_cell_v: float = Field(3.6, gt=0)


class TemperatureSensorConfig(SensorBaseConfig):
    type: Literal["temperature"] = "temperature"
    # linux_thermal reads /sys/class/thermal (e.g. the Pi 5 SoC temperature).
    driver: Literal["simulated", "linux_thermal", "external"] = "linux_thermal"
    rate_hz: float = Field(0.5, gt=0, le=20)
    stale_after_s: float | None = Field(10.0, gt=0)
    thermal_zone: int = Field(0, ge=0)
    warn_c: float = 75.0
    critical_c: float = 85.0


SensorConfig = Annotated[
    UltrasonicSensorConfig
    | IrObstacleSensorConfig
    | MetalDetectorSensorConfig
    | MagnetometerSensorConfig
    | ImuSensorConfig
    | WheelEncoderSensorConfig
    | BatteryMonitorSensorConfig
    | TemperatureSensorConfig,
    Field(discriminator="type"),
]


def default_sensors() -> list[Any]:
    return [
        UltrasonicSensorConfig(id="front_ultrasonic", label="Front ultrasonic"),
        IrObstacleSensorConfig(id="front_ir", label="Front IR"),
    ]


class GpsConfig(_Base):
    port: str = "/dev/ttyAMA0"
    baudrate: int = 9600
    min_fix_quality: int = Field(1, ge=1, le=8, description="NMEA GGA quality (1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float)")
    min_satellites: int = Field(5, ge=0)
    max_hdop: float = Field(2.5, gt=0)
    max_accuracy_m: float = Field(3.0, gt=0)
    max_fix_age_s: float = Field(2.0, gt=0)
    uere_m: float = Field(4.0, gt=0, description="Used to estimate accuracy from HDOP when GST is unavailable")
    cog_min_speed_mps: float = Field(0.15, ge=0)
    reconnect_interval_s: float = Field(3.0, gt=0)


class LocalizationConfig(_Base):
    provider: Literal["none", "simulated", "simulated_gps", "gps_nmea", "odometry"] = "none"
    gps: GpsConfig = GpsConfig()
    loss_pause_s: float = Field(1.0, ge=0)
    loss_abort_s: float = Field(60.0, gt=0)
    geo_max_extent_m: float = Field(2000.0, gt=0)
    max_approach_distance_m: float = Field(30.0, ge=0)


class CameraConfig(_Base):
    provider: Literal["none", "esp32cam", "simulated"] = "none"
    stream_url: str = "http://192.168.4.50:81/stream"
    snapshot_url: str = "http://192.168.4.50/capture"
    connect_timeout_s: float = Field(4.0, gt=0)
    read_timeout_s: float = Field(5.0, gt=0)
    max_backoff_s: float = Field(15.0, gt=0)
    max_frame_bytes: int = Field(800_000, gt=10_000)
    proxy_fps: float = Field(15.0, gt=0, le=30)


class VisionConfig(_Base):
    enabled: bool = False
    backend: Literal["onnx", "ultralytics"] = "onnx"
    model_path: str = "models/yolov8n.onnx"
    imgsz: int = Field(320, ge=160, le=1280)
    confidence: float = Field(0.35, ge=0.01, le=1.0)
    iou: float = Field(0.45, ge=0.05, le=0.95)
    max_rate_hz: float = Field(2.0, gt=0, le=30)
    threads: int = Field(2, ge=1, le=8)
    class_names: list[str] | None = None
    # Camera detections are only logged as events for these classes (empty = none).
    event_classes: list[str] = Field(default_factory=list)
    event_min_confidence: float = Field(0.6, ge=0, le=1.0)
    event_min_interval_s: float = Field(3.0, ge=0)
    weights_validated_for_task: bool = False
    model_description: str = "YOLOv8n (COCO-pretrained unless replaced with custom weights)"


class SimObstacle(_Base):
    type: Literal["circle", "rect"]
    x: float
    y: float
    r: float | None = None
    w: float | None = None
    h: float | None = None

    @model_validator(mode="after")
    def _shape(self) -> "SimObstacle":
        if self.type == "circle" and (self.r is None or self.r <= 0):
            raise ValueError("circle obstacles need r > 0")
        if self.type == "rect" and (not self.w or not self.h or self.w <= 0 or self.h <= 0):
            raise ValueError("rect obstacles need w > 0 and h > 0")
        return self


class SimMetalTarget(_Base):
    x: float
    y: float
    strength: float = Field(1.0, gt=0)
    radius_m: float = Field(0.25, gt=0)


class SimPose(_Base):
    x: float = 0.0
    y: float = 0.0
    heading_deg: float = 0.0


class GeoOrigin(_Base):
    lat: float = Field(12.9716, ge=-89.9, le=89.9)
    lon: float = Field(77.5946, ge=-180, le=180)
    alt: float = 900.0


class SimulationConfig(_Base):
    speed_factor: float = Field(1.0, ge=0.1, le=20.0)
    seed: int = 42
    physics_rate_hz: float = Field(50.0, ge=10, le=200)
    speed_bias: float = Field(0.04, ge=-0.5, le=0.5, description="True speed vs calibration (open-loop drift)")
    turn_bias: float = Field(0.06, ge=-0.5, le=0.5)
    wheel_noise: float = Field(0.02, ge=0, le=0.5)
    ultrasonic_noise_m: float = Field(0.008, ge=0)
    gps_noise_m: float = Field(0.03, ge=0, description="Simulated horizontal GPS noise (RTK-like by default)")
    start_pose: SimPose = SimPose()
    # Stand in for the operator placing the rover at the mission start pose. Disable
    # to exercise the GNSS approach and the start-distance check in simulation.
    place_rover_at_start: bool = True
    geo_origin: GeoOrigin = GeoOrigin()
    obstacles: list[SimObstacle] = Field(default_factory=list)
    metal_targets: list[SimMetalTarget] = Field(default_factory=list)


class MissionConfig(_Base):
    start_countdown_s: float = Field(3.0, ge=0, le=30)
    settle_time_s: float = Field(0.3, ge=0, le=5)
    pivot_output: float = Field(0.55, gt=0, le=1.0)
    turn_output: float = Field(0.45, gt=0, le=1.0)
    approach_output: float = Field(0.35, gt=0, le=1.0)
    max_lanes: int = Field(400, ge=1)
    max_segments: int = Field(5000, ge=10)
    lookahead_m: float = Field(0.35, gt=0)
    goal_tolerance_m: float = Field(0.12, gt=0)
    heading_tolerance_deg: float = Field(4.0, gt=0)
    progress_save_interval_s: float = Field(2.0, gt=0)


class UiConfig(_Base):
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_attribution: str = "&copy; OpenStreetMap contributors"
    map_max_zoom: int = 22
    # Where the GPS planner map opens when no GPS position is available.
    map_center_lat: float = Field(12.9716, ge=-90, le=90)
    map_center_lon: float = Field(77.5946, ge=-180, le=180)


class LoggingConfig(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_format: bool = False


class RoverConfig(_Base):
    mode: OperatingMode = OperatingMode.SIMULATION
    rover_name: str = "survey-rover"
    server: ServerConfig = ServerConfig()
    auth: AuthConfig = AuthConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    gpio: GpioConfig = GpioConfig()
    motors: MotorConfig = MotorConfig()
    motion: MotionConfig = MotionConfig()
    calibration: CalibrationConfig = CalibrationConfig()
    manual: ManualConfig = ManualConfig()
    safety: SafetyConfig = SafetyConfig()
    sensors: list[SensorConfig] = Field(default_factory=default_sensors)
    localization: LocalizationConfig = LocalizationConfig()
    camera: CameraConfig = CameraConfig()
    vision: VisionConfig = VisionConfig()
    simulation: SimulationConfig = SimulationConfig()
    mission: MissionConfig = MissionConfig()
    ui: UiConfig = UiConfig()

    @property
    def is_simulation(self) -> bool:
        return self.mode == OperatingMode.SIMULATION

    def sensor(self, sensor_id: str) -> Any | None:
        return next((s for s in self.sensors if s.id == sensor_id), None)

    def sensors_of_type(self, sensor_type: str, *, enabled_only: bool = True) -> list[Any]:
        return [s for s in self.sensors if s.type == sensor_type and (s.enabled or not enabled_only)]

    @model_validator(mode="after")
    def _mode_consistency(self) -> "RoverConfig":
        errors: list[str] = []
        sim = self.mode == OperatingMode.SIMULATION
        ids = [s.id for s in self.sensors]
        dup_ids = sorted({i for i in ids if ids.count(i) > 1})
        if dup_ids:
            errors.append(f"duplicate sensor ids: {dup_ids}")
        if sim:
            if self.motors.driver != "simulated":
                errors.append("simulation mode requires motors.driver=simulated")
            for s in self.sensors:
                if s.enabled and s.driver in GPIO_DRIVERS:
                    errors.append(f"simulation mode cannot use GPIO driver '{s.driver}' (sensor '{s.id}')")
            if self.localization.provider == "gps_nmea":
                errors.append("simulation mode cannot use the real GPS provider; use simulated_gps")
            if self.safety.estop_input.enabled:
                errors.append("simulation mode cannot read a GPIO e-stop input")
        else:
            if self.motors.driver == "simulated":
                errors.append(f"{self.mode} mode requires a real motor driver (l298n)")
            for s in self.sensors:
                if s.enabled and s.driver == "simulated":
                    errors.append(f"{self.mode} mode cannot use a simulated sensor ('{s.id}')")
            if self.camera.provider == "simulated":
                errors.append(f"{self.mode} mode cannot use a simulated camera")
            if self.localization.provider in ("simulated", "simulated_gps"):
                errors.append(f"{self.mode} mode cannot use simulated localization")
            if self.mode == OperatingMode.HARDWARE and self.localization.provider == "gps_nmea":
                errors.append("a GPS receiver is configured: use mode=hardware_gps")
            if self.mode == OperatingMode.HARDWARE_GPS and self.localization.provider != "gps_nmea":
                errors.append("hardware_gps mode requires localization.provider=gps_nmea")
        if self.localization.provider == "odometry":
            sides = {s.side for s in self.sensors_of_type("wheel_encoder")}
            if sides != {"left", "right"}:
                errors.append("localization.provider=odometry requires enabled left and right wheel_encoder sensors")
        for sid in self.safety.required_for_arming:
            sc = self.sensor(sid)
            if sc is None:
                errors.append(f"safety.required_for_arming lists unknown sensor '{sid}'")
            elif not sc.enabled:
                errors.append(f"safety.required_for_arming lists '{sid}' but that sensor is disabled")
        pins = self._used_pins()
        dupes = sorted({p for p in pins if pins.count(p) > 1})
        if not sim and dupes:
            errors.append(f"GPIO pins assigned more than once: {dupes}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _used_pins(self) -> list[int]:
        pins: list[int] = []
        if self.motors.driver == "l298n":
            for side in (self.motors.left, self.motors.right):
                pins += [side.in_a, side.in_b, side.enable]
        for s in self.sensors:
            if not s.enabled or s.driver not in GPIO_DRIVERS:
                continue
            for attr in ("trigger_pin", "echo_pin", "pin"):
                v = getattr(s, attr, None)
                if v is not None:
                    pins.append(v)
        if self.safety.estop_input.enabled:
            pins.append(self.safety.estop_input.pin)
        return pins

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if data["auth"].get("token"):
            data["auth"]["token"] = "***"
        return data

    def fingerprint(self) -> str:
        blob = json.dumps(self.redacted(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def load_config(path: str | os.PathLike[str] | None = None) -> RoverConfig:
    """Load and validate a configuration file. Raises on any validation error."""
    if path is None:
        path = os.environ.get("ROVER_CONFIG")
    data: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"configuration file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a mapping")
        data = loaded
    cfg = RoverConfig.model_validate(data)
    token = os.environ.get("ROVER_AUTH_TOKEN")
    if token:
        cfg.auth.token = token
    if cfg.auth.enabled and not cfg.auth.token:
        raise ValueError("auth.enabled is true but no token is configured (set ROVER_AUTH_TOKEN)")
    return cfg
