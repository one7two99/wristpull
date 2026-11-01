"""Build a small synthetic Gadgetbridge export with the real table layout (subset)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE DEVICE (_id INTEGER PRIMARY KEY, NAME TEXT NOT NULL, MANUFACTURER TEXT NOT NULL, IDENTIFIER TEXT NOT NULL UNIQUE, TYPE INTEGER NOT NULL, TYPE_NAME TEXT, MODEL TEXT, ALIAS TEXT, PARENT_FOLDER TEXT);
CREATE TABLE USER (_id INTEGER PRIMARY KEY, NAME TEXT NOT NULL, BIRTHDAY INTEGER NOT NULL, GENDER INTEGER NOT NULL);
CREATE TABLE HUAMI_EXTENDED_ACTIVITY_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, RAW_INTENSITY INTEGER NOT NULL, STEPS INTEGER NOT NULL, RAW_KIND INTEGER NOT NULL, HEART_RATE INTEGER NOT NULL, UNKNOWN1 INTEGER, SLEEP INTEGER, DEEP_SLEEP INTEGER, REM_SLEEP INTEGER, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_STRESS_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, TYPE_NUM INTEGER NOT NULL, STRESS INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_SPO2_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, TYPE_NUM INTEGER NOT NULL, SPO2 INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_HEART_RATE_RESTING_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, UTC_OFFSET INTEGER NOT NULL, HEART_RATE INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_SLEEP_SESSION_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, DATA BLOB, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, UTC_OFFSET INTEGER NOT NULL, RATE INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE HUAMI_PAI_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, UTC_OFFSET INTEGER NOT NULL, PAI_LOW REAL, PAI_MODERATE REAL, PAI_HIGH REAL, TIME_LOW INTEGER, TIME_MODERATE INTEGER, TIME_HIGH INTEGER, PAI_TODAY REAL, PAI_TOTAL REAL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE GENERIC_TEMPERATURE_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, TEMPERATURE REAL NOT NULL, TEMPERATURE_TYPE INTEGER NOT NULL, TEMPERATURE_LOCATION INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE GENERIC_HRV_VALUE_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, VALUE INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE GARMIN_STRESS_SAMPLE (TIMESTAMP INTEGER NOT NULL, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, STRESS INTEGER NOT NULL, PRIMARY KEY (TIMESTAMP, DEVICE_ID));
CREATE TABLE BASE_ACTIVITY_SUMMARY (_id INTEGER PRIMARY KEY, NAME TEXT, START_TIME INTEGER NOT NULL, END_TIME INTEGER NOT NULL, OVERRIDE_ACTIVITY_KIND INTEGER, ACTIVITY_KIND INTEGER NOT NULL, BASE_LONGITUDE INTEGER, BASE_LATITUDE INTEGER, BASE_ALTITUDE INTEGER, GPX_TRACK TEXT, RAW_DETAILS_PATH TEXT, HEADER_PHOTO TEXT, DEVICE_ID INTEGER NOT NULL, USER_ID INTEGER NOT NULL, SUMMARY_DATA TEXT, RAW_SUMMARY_DATA BLOB);
"""

GARMIN_MAC = "AA:BB:CC:DD:EE:01"
STRAP_MAC = "AA:BB:CC:DD:EE:02"
STAGE_CODES = {"light": 4, "deep": 5, "awake": 7, "rem": 8}


def build_sleep_session_blob(*, reported: datetime, midnight: datetime, stages: list[tuple[int, int, str]], score: int = 0, avg_hr: int = 0) -> bytes:
    """A 594-byte Huami sleep session report like Gadgetbridge stores it.

    ``stages`` are (start_minute, end_minute_inclusive, stage) with minutes counted from
    midnight of the day before ``midnight``.
    """
    blob = bytearray(594)
    blob[0:4] = int(reported.timestamp()).to_bytes(4, "little")
    blob[4:8] = int(midnight.timestamp()).to_bytes(4, "little")
    blob[8], blob[9] = 1, 1
    if stages:
        blob[0x0A:0x0C] = stages[0][0].to_bytes(2, "little")
        blob[0x0C:0x0E] = stages[-1][1].to_bytes(2, "little")
    blob[0x15], blob[0x16] = avg_hr, score
    blob[0x54] = len(stages)
    totals = {"rem": 0, "light": 0, "deep": 0, "awake": 0}
    for i, (start, end, stage) in enumerate(stages):
        offset = 0x56 + 5 * i
        blob[offset : offset + 2] = start.to_bytes(2, "little")
        blob[offset + 2 : offset + 4] = end.to_bytes(2, "little")
        blob[offset + 4] = STAGE_CODES[stage]
        totals[stage] += end - start + 1
    for offset, key in ((0x24A, "rem"), (0x24C, "light"), (0x24E, "deep"), (0x250, "awake")):
        blob[offset : offset + 2] = totals[key].to_bytes(2, "little")
    return bytes(blob)


def build_gb_db(path: Path, *, start: datetime | None = None, sleep_session: bool = True) -> Path:
    """Strap data for one evening/night/morning: 300 minutes, with a 90-minute sleep block.

    The sleep block (23:00-00:30 local) is present both as per-minute flags and, unless
    ``sleep_session`` is False, as the band's own sleep session report with stages.
    """
    start = start or datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # 21:00 local (CET after DST end on 2026-10-25)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO USER VALUES (1, 'Test', 0, 1)")
    conn.execute("INSERT INTO DEVICE VALUES (1, '(unknown)', 'Garmin', ?, 0, 'GARMIN_FENIX_6X', '6X Pro', 'fenix', NULL)", (GARMIN_MAC,))
    conn.execute("INSERT INTO DEVICE VALUES (2, 'Amazfit Helio Strap', 'Amazfit', ?, 0, 'AMAZFITHELIOSTRAP', '0.132.24.2', NULL, NULL)", (STRAP_MAC,))
    for i in range(300):
        t = start + timedelta(minutes=i)
        ts = int(t.timestamp())
        asleep = 120 <= i < 210  # 23:00 .. 00:30 local
        deep = asleep and 150 <= i < 180
        rem = asleep and 190 <= i < 200
        hr = 255 if i < 5 else (52 if asleep else 70 + i % 9)
        steps = 0 if asleep or i < 5 else (12 if i % 3 == 0 else 0)
        # real bands report RAW_KIND 120 while asleep and set bit 7 on the deep/rem bytes
        conn.execute(
            "INSERT INTO HUAMI_EXTENDED_ACTIVITY_SAMPLE VALUES (?, 2, 1, ?, ?, ?, ?, 15, ?, ?, ?)",
            (ts, 0 if asleep else 20, steps, 120 if asleep else 80, hr, 255 if asleep else 192, 128 + (50 if deep else 0), 128 + (60 if rem else 0)),
        )
        if i % 5 == 0:
            conn.execute("INSERT INTO GENERIC_TEMPERATURE_SAMPLE VALUES (?, 2, 1, ?, 2, 9)", (ts * 1000, 30.0 + (i % 7) / 10))
            conn.execute("INSERT INTO HUAMI_STRESS_SAMPLE VALUES (?, 2, 1, 0, ?)", (ts * 1000, 15 + i % 30))
    if sleep_session:
        # minutes since midnight of the previous local day (base 2026-10-30T23:00Z): 22:00Z = 1380
        midnight = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
        stages = [(1380, 1409, "light"), (1410, 1439, "deep"), (1440, 1449, "light"), (1450, 1459, "rem"), (1460, 1469, "light")]
        empty = build_sleep_session_blob(reported=start + timedelta(minutes=100), midnight=midnight, stages=[])
        conn.execute("INSERT INTO HUAMI_SLEEP_SESSION_SAMPLE VALUES (?, 2, 1, ?)", (int((start + timedelta(minutes=100)).timestamp()) * 1000, empty))
        blob = build_sleep_session_blob(reported=start + timedelta(minutes=215), midnight=midnight, stages=stages, score=70, avg_hr=52)
        conn.execute("INSERT INTO HUAMI_SLEEP_SESSION_SAMPLE VALUES (?, 2, 1, ?)", (int((start + timedelta(minutes=215)).timestamp()) * 1000, blob))
    conn.execute("INSERT INTO HUAMI_SPO2_SAMPLE VALUES (?, 2, 1, 0, 97)", (int((start + timedelta(minutes=160)).timestamp()) * 1000,))
    conn.execute("INSERT INTO HUAMI_HEART_RATE_RESTING_SAMPLE VALUES (?, 2, 1, 3600000, 51)", (int((start + timedelta(minutes=290)).timestamp()) * 1000,))
    conn.execute("INSERT INTO HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE VALUES (?, 2, 1, 3600000, 14)", (int((start + timedelta(minutes=170)).timestamp()) * 1000,))
    conn.execute("INSERT INTO GENERIC_HRV_VALUE_SAMPLE VALUES (?, 2, 1, 45)", (int((start + timedelta(minutes=175)).timestamp()) * 1000,))
    conn.execute("INSERT INTO HUAMI_PAI_SAMPLE VALUES (?, 2, 1, 3600000, 1.0, 2.0, 0.0, 10, 5, 0, 3.0, 41.5)", (int((start + timedelta(minutes=100)).timestamp()) * 1000,))
    # Garmin rows in the DB are ignored on purpose (FIT files are authoritative)
    conn.execute("INSERT INTO GARMIN_STRESS_SAMPLE VALUES (?, 1, 1, 30)", (int(start.timestamp()) * 1000,))
    w_start = start + timedelta(minutes=30)
    summary = {"distanceMeters": {"value": 3200.0, "unit": "meters"}, "caloriesBurnt": {"value": 210, "unit": "calories_unit"}, "averageHR": {"value": 128, "unit": "bpm"}, "maxHR": {"value": 151, "unit": "bpm"}, "activeSeconds": {"value": 1500, "unit": "seconds"}}
    conn.execute(
        "INSERT INTO BASE_ACTIVITY_SUMMARY VALUES (1, 'Evening walk', ?, ?, NULL, 64, NULL, NULL, NULL, NULL, NULL, NULL, 2, 1, ?, NULL)",
        (int(w_start.timestamp()) * 1000, int((w_start + timedelta(minutes=26)).timestamp()) * 1000, json.dumps(summary)),
    )
    # an accidental 3-minute start without any values (kind 0): must be skipped on import
    blip = start + timedelta(minutes=250)
    conn.execute(
        "INSERT INTO BASE_ACTIVITY_SUMMARY VALUES (2, NULL, ?, ?, NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, 2, 1, '{}', NULL)",
        (int(blip.timestamp()) * 1000, int((blip + timedelta(minutes=3)).timestamp()) * 1000),
    )
    conn.commit()
    conn.close()
    return path
