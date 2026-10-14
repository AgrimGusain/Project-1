"""Sensor driver registry.

Drivers register themselves for a ``(sensor_type, driver)`` pair. Adding a new
device is: (1) a config class in ``rover.config``; (2) a :class:`Sensor` subclass
decorated with :func:`register`; optionally (3) a capability definition and a
frontend widget. Motion control, planning and the mission manager do not change.

Real devices that are not yet built into this package plug in with
``driver: external`` and ``adapter: "my_package.module:MyDriver"``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rover.sensors.base import Sensor, SensorInitError

if TYPE_CHECKING:
    from rover.config import RoverConfig
    from rover.hal.gpio import LazyGpio
    from rover.sim.world import SimWorld


@dataclass
class DriverContext:
    rover_config: "RoverConfig"
    gpio: "LazyGpio | None" = None
    world: "SimWorld | None" = None


_REGISTRY: dict[tuple[str, str], type[Sensor]] = {}


def register(cls: type[Sensor]) -> type[Sensor]:
    key = (cls.sensor_type, cls.driver_name)
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ValueError(f"driver already registered for {key}")
    _REGISTRY[key] = cls
    return cls


def registered_drivers() -> dict[tuple[str, str], type[Sensor]]:
    _load_builtin()
    return dict(_REGISTRY)


def _load_external(cfg: Any) -> type[Sensor]:
    module_name, _, cls_name = cfg.adapter.partition(":")
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
    except (ImportError, AttributeError) as exc:
        raise SensorInitError(f"cannot load adapter '{cfg.adapter}': {exc}") from exc
    if not (isinstance(cls, type) and issubclass(cls, Sensor)):
        raise SensorInitError(f"adapter '{cfg.adapter}' is not a rover.sensors.base.Sensor subclass")
    if cls.sensor_type != cfg.type:
        raise SensorInitError(f"adapter '{cfg.adapter}' implements '{cls.sensor_type}', config expects '{cfg.type}'")
    if cls.simulated:
        raise SensorInitError("external adapters are real-device boundaries and must not be simulated")
    return cls


def create_sensor(cfg: Any, ctx: DriverContext) -> Sensor:
    """Instantiate the driver for a sensor config. Raises SensorInitError if unsupported."""
    _load_builtin()
    if cfg.driver == "external":
        cls = _load_external(cfg)
    else:
        cls = _REGISTRY.get((cfg.type, cfg.driver))
        if cls is None:
            raise SensorInitError(f"no driver '{cfg.driver}' for sensor type '{cfg.type}'")
    if cls.simulated and not ctx.rover_config.is_simulation:
        # Defence in depth: config validation already forbids this.
        raise SensorInitError(f"simulated driver refused outside simulation mode ('{cfg.id}')")
    if cls.simulated and ctx.world is None:
        raise SensorInitError("simulated driver requires the simulation world")
    return cls(cfg, ctx)


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from rover.sensors.drivers import gpio_drivers, linux, simulated  # noqa: F401

