"""Capability registry.

Each capability is a small, independently testable function that inspects the
*live* system (device status, readings, providers) — not just the configuration —
and returns a :class:`CapabilityReport`. New capabilities are added with the
:func:`capability` decorator; no central conditional needs editing.

Statuses:
* ``ready``           — available with real hardware
* ``simulated``       — available, but only in simulation (simulated devices)
* ``degraded``        — partially available; see reasons
* ``unavailable``     — configured/required device missing, failing or invalid
* ``not_installed``   — no device configured (hardware not fitted yet)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from rover.core.enums import LocalizationStatus
from rover.sensors.base import ConnectionState, ReadingHealth

if TYPE_CHECKING:
    from rover.runtime import RoverRuntime

Status = Literal["ready", "simulated", "degraded", "unavailable", "not_installed"]


@dataclass
class CapabilityReport:
    id: str
    label: str
    status: Status
    summary: str
    reasons: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in ("ready", "simulated", "degraded")


CapabilityFn = Callable[["RoverRuntime"], CapabilityReport]
_REGISTRY: list[tuple[str, CapabilityFn]] = []


def capability(cap_id: str) -> Callable[[CapabilityFn], CapabilityFn]:
    def deco(fn: CapabilityFn) -> CapabilityFn:
        if any(cid == cap_id for cid, _ in _REGISTRY):
            raise ValueError(f"capability {cap_id} already registered")
        _REGISTRY.append((cap_id, fn))
        return fn

    return deco


def evaluate_all(rt: "RoverRuntime") -> list[CapabilityReport]:
    out = []
    for cap_id, fn in _REGISTRY:
        try:
            out.append(fn(rt))
        except Exception as exc:  # a broken check must not hide the others
            out.append(CapabilityReport(cap_id, cap_id, "unavailable", f"capability check failed: {exc}"))
    return out


def evaluate(rt: "RoverRuntime", cap_id: str) -> CapabilityReport:
    for cid, fn in _REGISTRY:
        if cid == cap_id:
            return fn(rt)
    raise KeyError(cap_id)


def to_dicts(reports: list[CapabilityReport]) -> list[dict[str, Any]]:
    return [{**asdict(r), "usable": r.usable} for r in reports]


# --------------------------------------------------------------------------- helpers


def _sensor_cap(rt: "RoverRuntime", cap_id: str, label: str, sensor_type: str, what: str) -> CapabilityReport:
    channels = rt.sensors.of_type(sensor_type)
    if not channels:
        return CapabilityReport(cap_id, label, "not_installed", f"No {what} configured (hardware not installed).",
                                depends_on=[sensor_type])
    good, reasons = [], []
    for ch in channels:
        health = ch.reading_health()
        if ch.state == ConnectionState.CONNECTED and health == ReadingHealth.OK:
            good.append(ch)
        else:
            reasons.append(f"{ch.id}: {ch.state.value}/{health.value}" + (f" ({ch.error})" if ch.error else ""))
    if not good:
        return CapabilityReport(cap_id, label, "unavailable", f"{what.capitalize()} configured but not delivering valid data.",
                                reasons, [c.id for c in channels])
    simulated = all(c.simulated for c in good)
    status: Status = "degraded" if reasons else ("simulated" if simulated else "ready")
    note = " (simulated)" if simulated else ""
    if any(not c.physically_verified and not c.simulated for c in good):
        reasons.append("driver not yet verified on physical hardware")
    return CapabilityReport(cap_id, label, status, f"{len(good)}/{len(channels)} {what} OK{note}.", reasons,
                            [c.id for c in channels])


# --------------------------------------------------------------------------- definitions


@capability("manual_control")
def _manual(rt: "RoverRuntime") -> CapabilityReport:
    drv = rt.controller.driver.status()
    deps = ["motor_driver"]
    if not drv.get("healthy"):
        return CapabilityReport("manual_control", "Manual motor control", "unavailable",
                                "Motor driver is not available.", [drv.get("error") or "driver not initialised"], deps)
    if not rt.controller.loop_healthy:
        return CapabilityReport("manual_control", "Manual motor control", "unavailable", "Motion control loop is not running.", [], deps)
    status: Status = "simulated" if drv.get("simulated") else "ready"
    reasons = [] if rt.supervisor.armed else ["rover is disarmed: arm before driving"]
    return CapabilityReport("manual_control", "Manual motor control", status, "Tank-steer manual driving with dead-man.", reasons, deps)


@capability("obstacle_sensing")
def _obstacles(rt: "RoverRuntime") -> CapabilityReport:
    guards = [c for c in rt.sensors.channels.values() if c.cfg.enabled and getattr(c.cfg, "safety_role", "none") != "none"]
    if not guards:
        return CapabilityReport("obstacle_sensing", "Obstacle sensing", "not_installed",
                                "No obstacle sensors configured: forward motion is NOT protected.", depends_on=[])
    a = rt.supervisor.assessment
    ok = [c for c in guards if c.state == ConnectionState.CONNECTED and c.reading_health() == ReadingHealth.OK]
    reasons = [f"{c.id}: {c.state.value}/{c.reading_health().value}" for c in guards if c not in ok]
    if a and a.front.reasons:
        reasons += a.front.reasons
    if not ok:
        return CapabilityReport("obstacle_sensing", "Obstacle sensing", "unavailable",
                                "Obstacle sensors not delivering valid data; forward motion is blocked.", reasons, [c.id for c in guards])
    simulated = all(c.simulated for c in ok)
    status: Status = "degraded" if len(ok) < len(guards) else ("simulated" if simulated else "ready")
    return CapabilityReport("obstacle_sensing", "Obstacle sensing", status,
                            f"{len(ok)}/{len(guards)} obstacle sensors healthy. Not a landmine detector.", reasons, [c.id for c in guards])


@capability("grid_planning")
def _grid_planning(rt: "RoverRuntime") -> CapabilityReport:
    return CapabilityReport("grid_planning", "Grid route planning", "ready",
                            "Boustrophedon planning in a local grid. Needs no localization hardware.")


@capability("grid_execution")
def _grid_execution(rt: "RoverRuntime") -> CapabilityReport:
    manual = evaluate(rt, "manual_control")
    if manual.status == "unavailable":
        return CapabilityReport("grid_execution", "Grid mission execution", "unavailable", manual.summary, manual.reasons, ["manual_control"])
    est = rt.localization.estimate()
    sim = rt.config.is_simulation
    if est.kind == "local" and est.position_valid:
        summary = "Closed-loop on local pose" + (" (experimental odometry)" if est.experimental else "")
        status: Status = "simulated" if (sim or est.simulated) else ("degraded" if est.experimental else "ready")
        return CapabilityReport("grid_execution", "Grid mission execution", status, summary + ".", [], ["localization"])
    reasons = ["no local position source: execution is OPEN-LOOP (commanded motion, coverage not verified)"]
    if not rt.config.calibration.calibrated and not sim:
        reasons.append("open-loop calibration has not been measured")
    return CapabilityReport("grid_execution", "Grid mission execution", "simulated" if sim else "degraded",
                            "Open-loop execution from timed commands.", reasons, ["manual_control", "calibration"])


@capability("gps_planning")
def _gps_planning(rt: "RoverRuntime") -> CapabilityReport:
    return CapabilityReport("gps_planning", "GPS route planning", "ready",
                            "Polygon planning in a local ENU frame. Available without a receiver (planning only).")


@capability("gps_navigation")
def _gps_nav(rt: "RoverRuntime") -> CapabilityReport:
    p = rt.localization.provider
    if p.kind != "geodetic":
        return CapabilityReport("gps_navigation", "GPS-guided navigation", "not_installed",
                                "No GNSS receiver configured. GPS missions can be planned but not executed.", depends_on=["gps"])
    est = rt.localization.estimate()
    if not est.position_valid:
        return CapabilityReport("gps_navigation", "GPS-guided navigation", "unavailable",
                                f"GNSS {est.status.value}: {est.message}", [est.message], ["gps"])
    status: Status = "simulated" if p.simulated else "ready"
    return CapabilityReport("gps_navigation", "GPS-guided navigation", status,
                            f"Valid fix, accuracy {est.horizontal_accuracy_m or float('nan'):.2f} m.",
                            ["heading only from course-over-ground while moving; pivots are open-loop"], ["gps"])


@capability("encoder_odometry")
def _odometry(rt: "RoverRuntime") -> CapabilityReport:
    rep = _sensor_cap(rt, "encoder_odometry", "Wheel-encoder odometry", "wheel_encoder", "wheel encoders")
    sides = {c.cfg.side for c in rt.sensors.of_type("wheel_encoder")}
    if rep.status not in ("not_installed", "unavailable") and sides != {"left", "right"}:
        rep.status = "degraded"
        rep.reasons.append("both left and right encoders are required")
    if rep.usable:
        rep.reasons.append("experimental dead reckoning; tested in simulation only")
    return rep


@capability("heading_estimation")
def _heading(rt: "RoverRuntime") -> CapabilityReport:
    p = rt.localization.provider
    est = rt.localization.estimate()
    mags = rt.localization.magnetic_heading()
    reasons = [f"magnetometer {m['sensor_id']}: " + "; ".join(m["reasons"]) for m in mags if not m["reliable"]]
    if est.heading_valid:
        status: Status = "simulated" if est.simulated else ("degraded" if est.experimental else "ready")
        return CapabilityReport("heading_estimation", "Heading estimation", status, f"Heading from {est.heading_source}.", reasons, [p.name])
    if p.kind == "geodetic":
        return CapabilityReport("heading_estimation", "Heading estimation", "degraded",
                                "Heading only available from GNSS course-over-ground while moving.", reasons, [p.name])
    return CapabilityReport("heading_estimation", "Heading estimation", "unavailable",
                            "No trustworthy heading source. Magnetometer heading is diagnostic only.", reasons, [p.name, "magnetometer"])


@capability("magnetometer")
def _mag(rt: "RoverRuntime") -> CapabilityReport:
    rep = _sensor_cap(rt, "magnetometer", "Magnetometer measurements", "magnetometer", "magnetometers")
    for ch in rt.sensors.of_type("magnetometer"):
        if not ch.cfg.calibration.calibrated:
            rep.reasons.append(f"{ch.id}: not calibrated")
    return rep


@capability("imu")
def _imu(rt: "RoverRuntime") -> CapabilityReport:
    rep = _sensor_cap(rt, "imu", "IMU measurements", "imu", "IMUs")
    if rep.usable:
        rep.reasons.append("raw rates/accelerations only; orientation fusion not implemented")
    return rep


@capability("camera_streaming")
def _camera(rt: "RoverRuntime") -> CapabilityReport:
    st = rt.camera.status()
    if st["provider"] == "none":
        return CapabilityReport("camera_streaming", "Camera streaming", "not_installed", "No camera configured.")
    if not st["online"]:
        return CapabilityReport("camera_streaming", "Camera streaming", "unavailable",
                                f"Camera offline: {st.get('error') or 'no frames'}", [], ["camera"])
    return CapabilityReport("camera_streaming", "Camera streaming", "simulated" if st["simulated"] else "ready",
                            f"{st['provider']} streaming at {st.get('fps', 0):.1f} fps.", [], ["camera"])


@capability("object_detection")
def _vision(rt: "RoverRuntime") -> CapabilityReport:
    st = rt.vision.status()
    if not st["enabled"]:
        return CapabilityReport("object_detection", "Camera object detection (YOLOv8n)", "not_installed", "Vision disabled in configuration.")
    if not st["ready"]:
        return CapabilityReport("object_detection", "Camera object detection (YOLOv8n)", "unavailable", st.get("error") or "detector not ready", [], ["camera", "model"])
    reasons = ["model not validated for this task: detections are unverified candidates"] if not st["weights_validated_for_task"] else []
    cam = rt.camera.status()
    if not cam["online"]:
        return CapabilityReport("object_detection", "Camera object detection (YOLOv8n)", "degraded", "Model loaded but camera offline.", reasons, ["camera"])
    return CapabilityReport("object_detection", "Camera object detection (YOLOv8n)", "simulated" if cam["simulated"] else "ready",
                            f"{st['backend']} inference, {st.get('inference_ms') or 0:.0f} ms/frame.", reasons, ["camera", "model"])


@capability("detection_logging")
def _detection(rt: "RoverRuntime") -> CapabilityReport:
    rep = _sensor_cap(rt, "detection_logging", "Metal-detector event logging", "metal_detector", "metal detectors")
    if rep.usable:
        loc = rt.localization.estimate()
        if not loc.position_valid:
            rep.reasons.append("no valid position source: events are logged without a position")
        if rep.status == "simulated":
            rep.reasons.append("simulated detector: events are flagged SIMULATED")
    return rep


@capability("battery_monitoring")
def _battery(rt: "RoverRuntime") -> CapabilityReport:
    return _sensor_cap(rt, "battery_monitoring", "Battery monitoring", "battery", "battery monitors")


@capability("temperature_monitoring")
def _temperature(rt: "RoverRuntime") -> CapabilityReport:
    return _sensor_cap(rt, "temperature_monitoring", "Temperature monitoring", "temperature", "temperature sensors")


@capability("localization")
def _localization(rt: "RoverRuntime") -> CapabilityReport:
    est = rt.localization.estimate()
    p = rt.localization.provider
    if p.name == "none":
        return CapabilityReport("localization", "Physical localization", "not_installed",
                                "No GPS, encoders or other position source installed.", depends_on=[])
    if est.status == LocalizationStatus.OK and est.position_valid:
        status: Status = "simulated" if est.simulated else ("degraded" if est.experimental else "ready")
        return CapabilityReport("localization", "Physical localization", status, f"{p.name}: {est.message}", [], [p.name])
    return CapabilityReport("localization", "Physical localization", "unavailable", f"{p.name}: {est.status.value} — {est.message}", [], [p.name])
