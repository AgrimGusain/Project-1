import time

import pytest

from rover.config import GpsConfig
from rover.core.clock import utcnow
from rover.core.enums import LocalizationStatus
from rover.localization import nmea
from rover.localization.gnss import GnssFix, GnssState, GpsNmeaProvider

GGA = nmea.build("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
RMC = nmea.build("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W")
GGA_NOFIX = nmea.build("GNGGA,123520,,,,,0,00,99.9,,M,,M,,")


def test_parse_gga_and_rmc():
    g = nmea.parse(GGA)
    assert g.type == "GGA"
    assert g.data["lat"] == pytest.approx(48.1173)
    assert g.data["lon"] == pytest.approx(11.516667, abs=1e-6)
    assert g.data["quality"] == 1 and g.data["satellites"] == 8
    r = nmea.parse(RMC)
    assert r.data["valid"] and r.data["course_deg"] == pytest.approx(84.4)
    assert r.data["speed_mps"] == pytest.approx(22.4 * 0.514444)


def test_bad_checksum_and_garbage_rejected():
    with pytest.raises(nmea.NmeaError):
        nmea.parse(GGA[:-2] + "00")
    with pytest.raises(nmea.NmeaError):
        nmea.parse("hello")
    assert nmea.parse(nmea.build("GPGSV,1,1,00")) is None


def test_no_fix_never_reports_position():
    p = GpsNmeaProvider(GpsConfig())
    p.connected = True
    p.feed_line(GGA_NOFIX)
    est = p.estimate()
    assert est.status == LocalizationStatus.NO_FIX
    assert not est.position_valid and est.lat is None and est.lon is None


def test_valid_fix_and_quality_gates():
    p = GpsNmeaProvider(GpsConfig(max_hdop=2.5, uere_m=2.0, max_accuracy_m=3.0, min_satellites=5))
    p.connected = True
    p.feed_line(GGA)
    est = p.estimate()
    assert est.position_valid and est.status == LocalizationStatus.OK
    assert est.horizontal_accuracy_m == pytest.approx(1.8)
    assert est.accuracy_source == "hdop_estimate"
    # stationary/no RMC -> no heading
    assert not est.heading_valid and est.heading_deg is None


def test_degraded_fix_is_not_valid():
    st = GnssState(GpsConfig(max_hdop=2.0))
    st.update(GnssFix(48.0, 11.0, 0, 1, 9, 5.0, time.monotonic(), utcnow()))
    est = st.evaluate("t", False, True)
    assert est.status == LocalizationStatus.DEGRADED and not est.position_valid
    assert est.lat == 48.0  # raw value still visible, but flagged invalid


def test_stale_fix_is_not_valid():
    st = GnssState(GpsConfig(max_fix_age_s=1.0))
    st.update(GnssFix(48.0, 11.0, 0, 1, 9, 0.8, time.monotonic() - 5, utcnow()))
    assert not st.evaluate("t", False, True).position_valid


def test_heading_only_from_course_when_moving():
    cfg = GpsConfig(cog_min_speed_mps=0.2, uere_m=2.0)
    st = GnssState(cfg)
    st.update(GnssFix(48.0, 11.0, 0, 1, 9, 0.8, time.monotonic(), utcnow(), speed_mps=0.05, course_deg=90))
    assert not st.evaluate("t", False, True).heading_valid
    st.update(GnssFix(48.0, 11.0, 0, 1, 9, 0.8, time.monotonic(), utcnow(), speed_mps=0.5, course_deg=90))
    est = st.evaluate("t", False, True)
    assert est.heading_valid and est.heading_deg == 90 and est.heading_source == "course_over_ground"


def test_disconnected_receiver():
    p = GpsNmeaProvider(GpsConfig())
    est = p.estimate()
    assert est.status == LocalizationStatus.DISCONNECTED and not est.position_valid
    assert not est.simulated
