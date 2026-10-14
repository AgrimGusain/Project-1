"""Camera providers.

The ESP32-CAM serves an MJPEG stream (``/stream`` on port 81 with the bundled
firmware). An ESP32-CAM can only serve one or two stream clients reliably, so the
backend is the *single* consumer: it parses JPEG frames from the stream, keeps
the latest one, and re-serves it to any number of browsers (``/api/camera/stream``)
and to the vision pipeline.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rover.config import CameraConfig
from rover.core.clock import iso, utcnow

log = logging.getLogger(__name__)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    seq: int
    mono: float
    wall: datetime


class CameraProvider(ABC):
    provider = "none"
    simulated = False

    def __init__(self) -> None:
        self.latest: Frame | None = None
        self._seq = 0
        self._cond: asyncio.Condition | None = None
        self.error: str | None = None
        self._fps_window: list[float] = []
        self.frames_total = 0

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def _publish(self, jpeg: bytes) -> None:
        self._seq += 1
        now = time.monotonic()
        self.latest = Frame(jpeg, self._seq, now, utcnow())
        self.frames_total += 1
        self._fps_window = [t for t in self._fps_window if now - t < 3.0] + [now]
        cond = self._condition()
        async with cond:
            cond.notify_all()

    async def wait_frame(self, after_seq: int, timeout: float = 5.0) -> Frame | None:
        f = self.latest
        if f is not None and f.seq > after_seq:
            return f
        cond = self._condition()
        try:
            async with cond:
                await asyncio.wait_for(cond.wait_for(lambda: self.latest is not None and self.latest.seq > after_seq), timeout)
        except TimeoutError:
            return None
        return self.latest

    @property
    def online(self) -> bool:
        return self.latest is not None and time.monotonic() - self.latest.mono < 3.0

    @abstractmethod
    async def start(self) -> None: ...

    async def stop(self) -> None:  # noqa: B027
        pass

    def status(self) -> dict[str, Any]:
        f = self.latest
        fps = 0.0
        if len(self._fps_window) > 1:
            span = self._fps_window[-1] - self._fps_window[0]
            fps = (len(self._fps_window) - 1) / span if span > 0 else 0.0
        return {
            "provider": self.provider,
            "simulated": self.simulated,
            "online": self.online,
            "fps": round(fps, 1),
            "last_frame_at": iso(f.wall) if f else None,
            "last_frame_age_s": round(time.monotonic() - f.mono, 2) if f else None,
            "frames_total": self.frames_total,
            "error": self.error,
        }


class NoCamera(CameraProvider):
    provider = "none"

    async def start(self) -> None:
        self.error = "no camera configured"


class Esp32MjpegCamera(CameraProvider):
    provider = "esp32cam"
    simulated = False

    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self._task: asyncio.Task[None] | None = None
        self.reconnects = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="camera-esp32")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        import httpx

        backoff = 1.0
        timeout = httpx.Timeout(connect=self.cfg.connect_timeout_s, read=self.cfg.read_timeout_s, write=5.0, pool=5.0)
        while True:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("GET", self.cfg.stream_url) as resp:
                        resp.raise_for_status()
                        self.error = None
                        backoff = 1.0
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            await self._extract(buf)
                            if len(buf) > self.cfg.max_frame_bytes * 2:
                                del buf[: len(buf) - self.cfg.max_frame_bytes]
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.max_backoff_s)

    async def _extract(self, buf: bytearray) -> None:
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf.clear()
                return
            end = buf.find(EOI, start + 2)
            if end < 0:
                if start > 0:
                    del buf[:start]
                return
            jpeg = bytes(buf[start : end + 2])
            del buf[: end + 2]
            if len(jpeg) <= self.cfg.max_frame_bytes:
                await self._publish(jpeg)

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update({"stream_url": self.cfg.stream_url, "reconnects": self.reconnects})
        return st


class SimulatedCamera(CameraProvider):
    """Synthetic frames (Pillow) clearly watermarked SIMULATED."""

    provider = "simulated"
    simulated = True

    def __init__(self, world: Any, fps: float = 5.0) -> None:
        super().__init__()
        self.world = world
        self.fps = fps
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="camera-sim")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def render(self) -> bytes:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (320, 240), (34, 52, 40))
        d = ImageDraw.Draw(img)
        truth = self.world.truth()
        pose = truth["pose"]
        # crude horizon and ground texture that moves with the rover
        d.rectangle([0, 0, 320, 90], fill=(70, 90, 110))
        offset = int((pose["x"] * 40 + pose["y"] * 40) % 32)
        for i in range(-1, 11):
            y = 100 + i * 14 + offset % 14
            d.line([0, y, 320, y], fill=(48, 70, 52))
        d.rectangle([0, 0, 320, 18], fill=(150, 30, 30))
        d.text((6, 3), "SIMULATED CAMERA - NOT A REAL FEED", fill=(255, 255, 255))
        d.text((6, 200), f"x={pose['x']:.2f} y={pose['y']:.2f} hdg={pose['heading_deg']:.0f}", fill=(230, 230, 230))
        d.text((6, 216), time.strftime("%H:%M:%S"), fill=(230, 230, 230))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=70)
        return out.getvalue()

    async def _run(self) -> None:
        while True:
            if self.world.has_fault("camera_offline"):
                self.error = "simulated camera offline"
            else:
                self.error = None
                try:
                    await self._publish(await asyncio.to_thread(self.render))
                except Exception as exc:  # pragma: no cover
                    self.error = str(exc)
            await asyncio.sleep(1.0 / self.fps)


def build_camera(cfg: CameraConfig, world: Any | None) -> CameraProvider:
    if cfg.provider == "esp32cam":
        return Esp32MjpegCamera(cfg)
    if cfg.provider == "simulated":
        if world is None:
            raise ValueError("simulated camera requires simulation mode")
        return SimulatedCamera(world)
    return NoCamera()
