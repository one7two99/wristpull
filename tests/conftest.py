"""Synthetic FIT fixtures resembling a Garmin fenix 6 (no real data)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fitwriter import FitWriter, ts
from wristpull.config import Config
from wristpull.db import connect

TZ = "Europe/Berlin"
SERIAL = 3999999999
PRODUCT_FENIX6 = 3289  # garmin_product enum 'fenix6_sport' in the SDK profile


def file_id(w: FitWriter, file_type: int, created: datetime) -> None:
    w.message(
        0,
        [
            (0, "enum", file_type),
            (1, "uint16", 1),  # manufacturer garmin
            (2, "uint16", PRODUCT_FENIX6),
            (3, "uint32z", SERIAL),
            (4, "uint32", ts(created)),
        ],
    )


def build_monitor_fit(day: datetime | None = None) -> bytes:
    """One day of wellness data: steps (cumulative per activity type), HR via
    timestamp_16, stress + body battery, resting HR, SpO2, respiration."""
    day = day or datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # local midnight 2026-10-06 (CEST)
    w = FitWriter()
    file_id(w, 32, day)
    # monitoring_info: local timestamp is 2 h ahead in CEST
    w.message(103, [(253, "uint32", ts(day)), (0, "uint32", ts(day) + 7200), (1, "enum", 6), (5, "uint16", 1700)])
    steps_walk = 0
    steps_run = 0
    for i in range(0, 96):  # every 15 min
        t = day + timedelta(minutes=15 * i)
        if 28 <= i < 80:  # 7:00 .. 20:00 local
            steps_walk += 120
        if 44 <= i < 48:
            steps_run += 500
        # cumulative walking record (activity_type 6 = walking, cycles raw = steps)
        w.message(
            55,
            [
                (253, "uint32", ts(t)),
                (5, "enum", 6),
                (3, "uint32", steps_walk),
                (2, "uint32", steps_walk * 70),  # distance scale 100 -> 0.7 m/step
                (4, "uint32", steps_walk * 600),  # active_time ms
                (19, "uint16", steps_walk // 25),
            ],
        )
        if steps_run:
            w.message(
                55,
                [
                    (253, "uint32", ts(t)),
                    (5, "enum", 1),
                    (3, "uint32", steps_run),
                    (2, "uint32", steps_run * 110),
                    (4, "uint32", steps_run * 400),
                    (19, "uint16", steps_run // 10),
                ],
            )
        # heart rate samples with timestamp_16 every 2 min after the full timestamp
        for j in range(1, 8):
            t_hr = t + timedelta(minutes=2 * j)
            hr = 55 + (i % 7) * 3 + j
            if 44 <= i < 48:
                hr += 70
            w.message(55, [(26, "uint16", ts(t_hr) & 0xFFFF), (27, "uint8", hr)])
        # stress + body battery every 15 min (stress -1 when unmeasurable)
        stress = -1 if i % 13 == 0 else (20 + (i * 7) % 60)
        bb = max(5, min(100, 90 - i // 2 if i < 80 else 50 + (i - 80) * 2))
        w.message(227, [(0, "sint16", stress), (1, "uint32", ts(t)), (2, "sint16", 30), (3, "uint8", bb)])
    # day-end cumulative record stamped exactly at next local midnight
    midnight = day + timedelta(days=1)
    w.message(55, [(253, "uint32", ts(midnight)), (5, "enum", 6), (3, "uint32", steps_walk), (2, "uint32", steps_walk * 70), (4, "uint32", steps_walk * 600), (19, "uint16", steps_walk // 25)])
    w.message(55, [(253, "uint32", ts(midnight)), (5, "enum", 6), (3, "uint32", 0), (2, "uint32", 0), (4, "uint32", 0), (19, "uint16", 0)])
    w.message(55, [(253, "uint32", ts(day + timedelta(hours=20))), (33, "uint16", 25), (34, "uint16", 10)])
    w.message(211, [(253, "uint32", ts(day + timedelta(hours=23))), (0, "uint8", 48), (1, "uint8", 47)])
    w.message(269, [(253, "uint32", ts(day + timedelta(hours=3))), (0, "uint8", 96), (1, "uint8", 2), (2, "enum", 3)])
    w.message(297, [(253, "uint32", ts(day + timedelta(hours=3))), (0, "sint16", 1450)])
    return w.finish()


def build_sleep_fit(start: datetime | None = None, *, file_type: int = 32) -> bytes:
    """``file_type`` 32 as the fenix writes it into the Sleep folder; the Connect export carries 49."""
    start = start or datetime(2026, 10, 5, 21, 30, tzinfo=UTC)  # 23:30 local, wake 2026-10-06
    w = FitWriter()
    file_id(w, file_type, start)
    # the watch's sleep window (event 74 start/stop) opens 20 minutes before the first stage record
    w.message(21, [(253, "uint32", ts(start - timedelta(minutes=20))), (0, "enum", 74), (1, "enum", 0)])
    stages = [(0, 1), (10, 2), (40, 3), (90, 2), (120, 4), (150, 2), (300, 3), (340, 2), (420, 1), (425, 2), (470, 1)]
    for minutes, level in stages:
        w.message(275, [(253, "uint32", ts(start + timedelta(minutes=minutes))), (0, "enum", level)])
    w.message(21, [(253, "uint32", ts(start + timedelta(minutes=470))), (0, "enum", 74), (1, "enum", 1)])
    w.message(346, [(6, "uint8", 78), (7, "uint8", 80), (8, "uint8", 70), (11, "uint8", 2), (15, "uint16", 2150)])
    return w.finish()


def build_activity_fit(start: datetime | None = None, *, with_gps: bool = True) -> bytes:
    start = start or datetime(2026, 10, 6, 6, 0, tzinfo=UTC)  # 08:00 local
    w = FitWriter()
    file_id(w, 4, start)
    w.message(21, [(253, "uint32", ts(start)), (0, "enum", 0), (1, "enum", 0), (3, "uint32", 0)])  # timer start
    n = 1800  # 30 min of 1 s records
    lat0, lon0 = 52.52, 13.405
    for i in range(0, n, 5):
        t = start + timedelta(seconds=i)
        fields = [
            (253, "uint32", ts(t)),
            (3, "uint8", 120 + (i // 60) % 40),
            (5, "uint32", int(i * 2.8 * 100)),
            (73, "uint32", 2800),
            (4, "uint8", 85),
            (78, "uint32", (540 + 500) * 5 + i // 100),  # enhanced_altitude: scale 5, offset 500
            (13, "sint8", 14),
        ]
        if with_gps:
            fields += [(0, "sint32", int((lat0 + i * 1e-5) / 180 * 2**31)), (1, "sint32", int((lon0 + i * 1e-5) / 180 * 2**31))]
        w.message(20, fields)
    end = start + timedelta(seconds=n)
    for li in range(3):
        w.message(
            19,
            [
                (253, "uint32", ts(start + timedelta(seconds=(li + 1) * 600))),
                (254, "uint16", li),
                (2, "uint32", ts(start + timedelta(seconds=li * 600))),
                (7, "uint32", 600000),
                (8, "uint32", 600000),
                (9, "uint32", 168000),
                (15, "uint8", 140 + li),
                (16, "uint8", 160 + li),
                (11, "uint16", 110),
            ],
        )
    w.message(
        18,
        [
            (253, "uint32", ts(end)),
            (2, "uint32", ts(start)),
            (5, "enum", 1),  # running
            (6, "enum", 2),  # street
            (7, "uint32", n * 1000),
            (8, "uint32", n * 1000),
            (9, "uint32", 504000),
            (11, "uint16", 330),
            (16, "uint8", 141),
            (17, "uint8", 168),
            (124, "uint32", 2800),
            (125, "uint32", 3900),
            (22, "uint16", 45),
            (23, "uint16", 40),
            (18, "uint8", 85),
            (24, "uint8", 33),  # aerobic TE 3.3
            (137, "uint8", 12),
            (168, "sint32", int(61.5 * 65536)),
            (26, "uint16", 3),
            (65, "uint32", [0, 120000, 900000, 600000, 180000, 0]),
            (110, "string", "Run"),
        ],
    )
    w.message(34, [(253, "uint32", ts(end)), (0, "uint32", n * 1000), (1, "uint16", 1), (2, "enum", 0), (3, "enum", 26), (4, "enum", 1)])
    return w.finish()


def build_metrics_fit(when: datetime | None = None) -> bytes:
    when = when or datetime(2026, 10, 6, 6, 40, tzinfo=UTC)
    w = FitWriter()
    file_id(w, 44, when)
    w.message(229, [(0, "uint32", ts(when)), (2, "uint16", 523), (5, "enum", 1)])
    return w.finish()


def build_hrv_fit(when: datetime | None = None) -> bytes:
    when = when or datetime(2026, 10, 6, 5, 0, tzinfo=UTC)
    w = FitWriter()
    file_id(w, 68, when)
    for i in range(6):
        w.message(371, [(253, "uint32", ts(when - timedelta(minutes=5 * (6 - i)))), (0, "uint16", int((38 + i) * 128))])
    w.message(370, [(253, "uint32", ts(when)), (0, "uint16", 42 * 128), (1, "uint16", 40 * 128), (2, "uint16", 55 * 128), (3, "uint16", 35 * 128), (4, "uint16", 38 * 128), (5, "uint16", 50 * 128), (6, "enum", 4)])
    return w.finish()


class _NoRealAdb:
    """Guard: tests must never talk to a real phone."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("real adb must not be used in tests; inject a FakeAdb")


@pytest.fixture(autouse=True)
def _block_real_adb(monkeypatch):
    import wristpull.importers.phone as phone_module

    monkeypatch.setattr(phone_module, "Adb", _NoRealAdb)


def _no_real_mtp(argv: list[str]) -> tuple[int, str, str]:
    raise AssertionError(f"real mtp-tools must not be used in tests ({argv[0]}); inject a FakeMtp or fake_mtp_runner")


@pytest.fixture(autouse=True)
def _block_real_mtp(monkeypatch):
    """Guard: tests must never talk to a real watch; ``[garmin] access = auto`` stays gvfs outside the container."""
    import wristpull.mtp as mtp_module

    monkeypatch.setattr(mtp_module, "run_command", _no_real_mtp)
    monkeypatch.delenv("WRISTPULL_IN_CONTAINER", raising=False)


@pytest.fixture
def monitor_fit() -> bytes:
    return build_monitor_fit()


@pytest.fixture
def sleep_fit() -> bytes:
    return build_sleep_fit()


@pytest.fixture
def activity_fit() -> bytes:
    return build_activity_fit()


@pytest.fixture
def garmin_tree(tmp_path: Path) -> Path:
    """A fake mounted watch: <tmp>/fenix/GARMIN/{Activity,Monitor,Sleep,Metrics,HRVStatus}."""
    root = tmp_path / "fenix" / "GARMIN"
    (root / "Activity").mkdir(parents=True)
    (root / "Monitor").mkdir()
    (root / "Sleep").mkdir()
    (root / "Metrics").mkdir()
    (root / "HRVStatus").mkdir()
    (root / "Settings").mkdir()
    (root / "Activity" / "2026-10-06-08-00-00.fit").write_bytes(build_activity_fit())
    (root / "Monitor" / "M6A10500.FIT").write_bytes(build_monitor_fit())
    (root / "Sleep" / "S6A10530.FIT").write_bytes(build_sleep_fit())
    (root / "Metrics" / "MET00001.FIT").write_bytes(build_metrics_fit())
    (root / "HRVStatus" / "HRV00001.FIT").write_bytes(build_hrv_fit())
    (root / "Settings" / "Settings.fit").write_bytes(b"not a fit file")
    (root / "GarminDevice.xml").write_text(
        '<?xml version="1.0"?><Device xmlns="http://www.garmin.com/xmlschemas/GarminDevice/v2">'
        "<Model><PartNumber>006-B3289-00</PartNumber><Description>fenix 6 Pro</Description></Model>"
        "<Id>3999999999</Id></Device>",
        "utf-8",
    )
    return root


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "Health", timezone=TZ)


@pytest.fixture
def conn(config: Config):
    connection = connect(config.db_path)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _block_real_ble(monkeypatch):
    """No test may open Bluetooth: the default transport factory, the scanner and bleak's connect are stubs."""
    import wristpull.zeppos.transport as transport

    def _blocked(*_args, **_kwargs):
        raise AssertionError("tests must not touch Bluetooth; inject FakeTransport (tests/fake_band.py)")

    monkeypatch.delenv("WRISTPULL_STRAP_AUTHKEY", raising=False)  # the developer's own key must not leak into tests
    monkeypatch.setattr(transport, "open_transport", _blocked)
    monkeypatch.setattr(transport, "scan", _blocked)
    monkeypatch.setattr(transport.BleakTransport, "connect", _blocked)
