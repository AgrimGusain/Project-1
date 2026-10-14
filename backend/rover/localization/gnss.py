"""GNSS localization: real NMEA receivers and a simulated receiver share one evaluator."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rover.config import GeoOrigin, GpsConfig
from rover.core.clock import utcnow
from rover.core.enums import LocalizationStatus
from rover.localization import nmea
from rover.localization.base import LocalizationEstimate, LocalizationProvider, unavailable
from rover.planning.coverage import compass_of
from rover.planning.geo import EnuFrame, haversine_m

log = logging.getLogger(__name__)


@dataclass
class GnssFix:
    lat: float | None
    lon: float | None
    alt: float | None
    quality: int
    satellites: int | None
    hdop: float | None
    mono: float
    wall: datetime
    speed_mps: float | None = None
    course_deg: float | None = None
    std_lat: float | None = None
    std_lon: float | None = None


class GnssState:
    """Accumulates fixes and evaluates validity against the configured quality gates."""

    def __init__(self, cfg: GpsConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self.fix: GnssFix | None = None
        self._prev_valid: GnssFix | None = None
        self._cog: tuple[float, float, float] | None = None  # (course, speed, mono)
        self.std: tuple[float | None, float | None, float] | None = None

    def update(self, fix: GnssFix) -> None:
        with self._lock:
            if fix.lat is not None and fix.lon is not None and fix.quality > 0:
                prev = self._prev_valid
                if fix.course_deg is not None and fix.speed_mps is not None:
                    self._cog = (fix.course_deg, fix.speed_mps, fix.mono)
                elif prev is not None and fix.mono - prev.mono >= 1.0:
                    assert prev.lat is not None and prev.lon is not None
                    dist = haversine_m(prev.lat, prev.lon, fix.lat, fix.lon)
                    dt = fix.mono - prev.mono
                    frame = EnuFrame(prev.lat, prev.lon)
                    e, n = frame.to_enu(fix.lat, fix.lon)
                    self._cog = (compass_of(e, n), dist / dt, fix.mono)
                if prev is None or fix.mono - prev.mono >= 1.0:
                    self._prev_valid = fix
            self.fix = fix

    def accuracy(self, fix: GnssFix) -> tuple[float | None, str | None]:
        if fix.std_lat is not None and fix.std_lon is not None:
            return math.hypot(fix.std_lat, fix.std_lon), "receiver_gst"
        if self.std and self.std[0] is not None and self.std[1] is not None and fix.mono - self.std[2] < 3.0:
            return math.hypot(self.std[0], self.std[1]), "receiver_gst"
        if fix.hdop is not None:
            return fix.hdop * self.cfg.uere_m, "hdop_estimate"
        return None, None

    def evaluate(self, provider: str, simulated: bool, connected: bool, conn_msg: str = "") -> LocalizationEstimate:
        if not connected:
            return unavailable(provider, conn_msg or "GPS receiver not connected", LocalizationStatus.DISCONNECTED,
                               simulated=simulated, kind="geodetic")
        with self._lock:
            fix, cog = self.fix, self._cog
        if fix is None:
            return unavailable(provider, "waiting for NMEA data", LocalizationStatus.STARTING, simulated=simulated, kind="geodetic")
        age = time.monotonic() - fix.mono
        base: dict[str, Any] = dict(
            provider=provider, kind="geodetic", simulated=simulated, fix_quality=fix.quality,
            satellites=fix.satellites, hdop=fix.hdop, timestamp=fix.wall, monotonic=fix.mono,
        )
        if fix.quality <= 0 or fix.lat is None or fix.lon is None:
            # Never report a position without a fix.
            return LocalizationEstimate(status=LocalizationStatus.NO_FIX, message="receiver has no valid fix", **base)
        acc, acc_src = self.accuracy(fix)
        problems = []
        if age > self.cfg.max_fix_age_s:
            problems.append(f"fix is {age:.1f} s old")
        if fix.quality < self.cfg.min_fix_quality:
            problems.append(f"fix quality {fix.quality} < {self.cfg.min_fix_quality}")
        if fix.satellites is not None and fix.satellites < self.cfg.min_satellites:
            problems.append(f"{fix.satellites} satellites < {self.cfg.min_satellites}")
        if fix.hdop is not None and fix.hdop > self.cfg.max_hdop:
            problems.append(f"HDOP {fix.hdop:.1f} > {self.cfg.max_hdop}")
        if acc is not None and acc > self.cfg.max_accuracy_m:
            problems.append(f"accuracy {acc:.2f} m > {self.cfg.max_accuracy_m} m")
        valid = not problems
        heading_valid = False
        heading = speed = None
        if cog is not None and time.monotonic() - cog[2] < 2.0:
            speed = cog[1]
            if valid and cog[1] >= self.cfg.cog_min_speed_mps:
                heading_valid, heading = True, cog[0] % 360.0
        return LocalizationEstimate(
            status=LocalizationStatus.OK if valid else LocalizationStatus.DEGRADED,
            message="; ".join(problems) if problems else "valid fix",
            position_valid=valid,
            lat=fix.lat, lon=fix.lon, alt=fix.alt,
            horizontal_accuracy_m=acc, accuracy_source=acc_src,
            heading_valid=heading_valid, heading_deg=heading,
            heading_source="course_over_ground" if heading_valid else None,
            speed_mps=speed,
            **base,
        )


class GpsNmeaProvider(LocalizationProvider):
    """Real NMEA receiver on a serial port (UART or USB). Implemented, not yet physically verified."""

    name = "gps_nmea"
    kind = "geodetic"
    simulated = False

    def __init__(self, cfg: GpsConfig) -> None:
        self.cfg = cfg
        self.state = GnssState(cfg)
        self.connected = False
        self.error: str | None = "not started"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, Any] = {}
        self.sentences = 0
        self.bad_sentences = 0

    async def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gps-reader", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 2.0)

    def _run(self) -> None:
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError:
            self.error = "pyserial is not installed"
            return
        while not self._stop.is_set():
            try:
                with serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=1.0) as port:
                    self.connected, self.error = True, None
                    log.info("GPS connected on %s", self.cfg.port)
                    last_data = time.monotonic()
                    while not self._stop.is_set():
                        raw = port.readline()
                        if not raw:
                            if time.monotonic() - last_data > 5.0:
                                raise OSError("no data from receiver for 5 s")
                            continue
                        last_data = time.monotonic()
                        self.feed_line(raw.decode("ascii", errors="replace"))
            except Exception as exc:
                if self.connected or self.error != str(exc):
                    log.warning("GPS receiver unavailable: %s", exc)
                self.connected, self.error = False, str(exc)
                self._stop.wait(self.cfg.reconnect_interval_s)

    def feed_line(self, line: str) -> None:
        try:
            s = nmea.parse(line)
        except nmea.NmeaError:
            self.bad_sentences += 1
            return
        if s is None:
            return
        self.sentences += 1
        now, wall = time.monotonic(), utcnow()
        if s.type == "GST":
            self.state.std = (s.data["std_lat"], s.data["std_lon"], now)
        elif s.type == "RMC":
            self._pending = s.data
        elif s.type == "GGA":
            d = s.data
            rmc = self._pending if self._pending.get("time") == d["time"] and self._pending.get("valid") else {}
            self.state.update(GnssFix(
                lat=d["lat"], lon=d["lon"], alt=d["alt"], quality=d["quality"], satellites=d["satellites"],
                hdop=d["hdop"], mono=now, wall=wall, speed_mps=rmc.get("speed_mps"), course_deg=rmc.get("course_deg"),
            ))

    def estimate(self) -> LocalizationEstimate:
        est = self.state.evaluate(self.name, False, self.connected, self.error or "")
        est.extra = {"port": self.cfg.port, "sentences": self.sentences, "bad_sentences": self.bad_sentences}
        return est


class SimulatedGpsProvider(LocalizationProvider):
    """Synthesises GNSS fixes from simulation truth. Always reported as simulated."""

    name = "simulated_gps"
    kind = "geodetic"
    simulated = True

    def __init__(self, cfg: GpsConfig, world: Any, origin: GeoOrigin, noise_m: float, rate_hz: float = 5.0) -> None:
        self.cfg = cfg
        self.world = world
        self.frame = EnuFrame(origin.lat, origin.lon, origin.alt)
        self.origin = origin
        self.noise_m = noise_m
        self.rate_hz = rate_hz
        self.state = GnssState(cfg)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sim-gps")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def sample(self) -> None:
        w = self.world
        now, wall = time.monotonic(), utcnow()
        if w.has_fault("gps_loss"):
            self.state.update(GnssFix(None, None, None, 0, 3, None, now, wall))
            return
        degraded = w.has_fault("gps_degraded")
        sigma = 4.0 if degraded else self.noise_m
        truth = w.truth()
        pose = truth["pose"]
        e = pose["x"] + w.gauss(sigma)
        n = pose["y"] + w.gauss(sigma)
        lat, lon = self.frame.to_geodetic(e, n)
        # Receivers derive speed/course from Doppler, not position differences.
        v = truth["speed_mps"]
        course = (pose["heading_deg"] + (180.0 if v < 0 else 0.0) + w.gauss(1.0)) % 360.0
        self.state.update(GnssFix(
            speed_mps=abs(v) + abs(w.gauss(0.01)), course_deg=course,
            lat=lat, lon=lon, alt=self.origin.alt, quality=1 if degraded else 4,
            satellites=6 if degraded else 14, hdop=6.0 if degraded else 0.7, mono=now, wall=wall,
            std_lat=sigma / math.sqrt(2) if not degraded else 5.0, std_lon=sigma / math.sqrt(2) if not degraded else 5.0,
        ))

    async def _run(self) -> None:
        while True:
            self.sample()
            # Fix rate follows simulation time so faster-than-real-time runs stay realistic.
            scale = getattr(self.world.clock, "scale", 1.0) or 1.0
            await asyncio.sleep(1.0 / (self.rate_hz * scale))

    def estimate(self) -> LocalizationEstimate:
        return self.state.evaluate(self.name, True, True)
