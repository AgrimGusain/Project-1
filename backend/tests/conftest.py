from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from rover.config import RoverConfig
from rover.runtime import RoverRuntime


def make_config(**overrides: Any) -> RoverConfig:
    """Simulation config tuned for fast, deterministic tests."""
    base: dict[str, Any] = {
        "mode": "simulation",
        "server": {"frontend_dist": None},
        "simulation": {"speed_factor": 1.0, "seed": 1, "speed_bias": 0.0, "turn_bias": 0.0, "wheel_noise": 0.0,
                       "ultrasonic_noise_m": 0.0},
        "mission": {"start_countdown_s": 0.0, "settle_time_s": 0.05},
        "safety": {"auto_disarm_idle_s": 0},
        "camera": {"provider": "none"},
        "sensors": [
            {"id": "front_ultrasonic", "type": "ultrasonic", "driver": "simulated", "safety_role": "front"},
            {"id": "front_ir", "type": "ir_obstacle", "driver": "simulated", "safety_role": "front"},
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return RoverConfig.model_validate(base)


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


@pytest.fixture
def config_factory() -> Callable[..., RoverConfig]:
    return make_config


@pytest.fixture
async def runtime_factory(tmp_path) -> AsyncIterator[Callable[..., Any]]:  # type: ignore[no-untyped-def]
    started: list[RoverRuntime] = []

    async def factory(cfg: RoverConfig | None = None) -> RoverRuntime:
        rt = RoverRuntime(cfg or make_config(), str(tmp_path / f"rover{len(started)}.db"))
        await rt.start()
        started.append(rt)
        return rt

    yield factory
    for rt in started:
        await rt.stop()


@pytest.fixture
async def runtime(runtime_factory) -> RoverRuntime:  # type: ignore[no-untyped-def]
    return await runtime_factory()
