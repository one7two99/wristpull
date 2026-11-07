"""``pull strap``: the importer against the fake band, from first sync to reparse."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fake_band import FAKE_ADDRESS, FAKE_KEY, FakeBandState, FakeTransport
from zepp_buffers import activity_minutes, build_sleep_session_blob, hr_max_record, hrv_record, pai_record, respiration_record, resting_hr_record, spo2_record, stress_minutes, workout_details, workout_summary
from wristpull.config import Config
from wristpull.db import connect, table_count
from wristpull.importers.reparse import reparse_raw_file, select_raw_files
from wristpull.importers.strap import import_buffer, pull_strap, read_auth_key, since_for
from wristpull.zeppos import SOURCE
from wristpull.zeppos.band import BandError, NoBandError
from wristpull.zeppos.fetch import BY_NAME, Buffer
from wristpull.zeppos.store import all_sync, clear_sync, last_sync

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)  # 19:00 CET
FIRST = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)  # local midnight 2026-11-06


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(data_dir=tmp_path / "data", timezone="Europe/Berlin", strap_address=FAKE_ADDRESS, strap_since_days=10)
    config.strap_authkey_path.parent.mkdir(parents=True)
    config.strap_authkey_path.write_text("0x" + FAKE_KEY.hex() + "\n")
    return config


@pytest.fixture
def band() -> FakeBandState:
    state = FakeBandState()
    # two local days of minutes (06.11. 00:00 .. 07.11. 18:59 CET) with a sleep window flagged in the first night
    state.minutes["activity"] = (FIRST, activity_minutes(24 * 60, steps=3, hr=65) + activity_minutes(19 * 60, steps=4, hr=72))
    state.minutes["stress"] = (FIRST, stress_minutes([None] * 59 + [30] + [None] * (43 * 60 - 60)))
    state.records["spo2"] = [(FIRST + timedelta(hours=2), spo2_record(FIRST + timedelta(hours=2), 96))]
    state.records["hrv"] = [(FIRST + timedelta(hours=3), hrv_record(FIRST + timedelta(hours=3), 48))]
    state.records["resting_hr"] = [(FIRST + timedelta(hours=8), resting_hr_record(FIRST + timedelta(hours=8), 52))]
    state.records["respiration"] = [(FIRST + timedelta(hours=3), respiration_record(FIRST + timedelta(hours=3), 15))]
    midnight = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)
    stages = [(1380, 1409, "light"), (1410, 1439, "deep"), (1440, 1449, "light"), (1450, 1479, "rem")]
    state.records["pai"] = [(FIRST + timedelta(hours=1), pai_record(FIRST + timedelta(hours=1), today=2.5, total=40.0))]
    state.records["hr_max"] = [(FIRST + timedelta(hours=10), hr_max_record(FIRST + timedelta(hours=10), 151))]
    w1 = FIRST + timedelta(hours=9)
    w2 = FIRST + timedelta(hours=30)
    state.workouts = [
        (w1, workout_summary(start=w1, type_code=0x01, total_s=1800), workout_details(int(w1.timestamp() * 1000), seconds=20)),
        (w2, workout_summary(start=w2, type_code=0xDF, auto=True, total_s=900, distance_m=None, pace=None, steps=None, training=None), b""),
    ]
    state.records["sleep"] = [(FIRST + timedelta(hours=7), build_sleep_session_blob(reported=FIRST + timedelta(hours=7), midnight=midnight, stages=stages, score=71, avg_hr=50))]
    return state


def run_pull(cfg: Config, state: FakeBandState, **kwargs):
    logs: list[str] = []
    conn = connect(cfg.db_path)
    transports: list[FakeTransport] = []
    present = kwargs.pop("present", True)

    def factory(address: str) -> FakeTransport:
        assert address == FAKE_ADDRESS
        t = FakeTransport(state, present=present)
        transports.append(t)
        return t

    stats = pull_strap(conn, cfg, transport_factory=factory, log=logs.append, clock=lambda: NOW, **kwargs)
    return conn, stats, logs, transports[0]


def test_first_pull_imports_every_kind(cfg: Config, band: FakeBandState):
    conn, stats, logs, transport = run_pull(cfg, band)
    assert band.authenticated and stats.device and "3.25.0.3" in stats.device and stats.address == FAKE_ADDRESS
    assert stats.errors == 0 and stats.files_new == 11 and stats.fetched["activity"] == 43 * 60 and stats.fetched["pai"] == 1 and stats.fetched["workout"] == 2
    assert stats.rows["activities"] == 2 and stats.rows["activity_records"] == 20 and stats.rows["metrics"] == 4  # pai_total, pai_today, hr_max, vo2_max
    sports = [tuple(r) for r in conn.execute("SELECT sport, sub_sport, elapsed_s FROM activities WHERE source = ? ORDER BY start_utc", (SOURCE,))]
    assert sports == [("running", None, 1800.0), ("auto_detected", "auto_detected", 900.0)]
    asked = [since for name, since in band.fetch_requests if name in ("workout", "workout_details")]
    # summary since 10 days back, details for that start, then the next summary after the workout's end, and so on
    assert asked == [NOW - timedelta(days=10), FIRST + timedelta(hours=9), FIRST + timedelta(hours=9, minutes=30), FIRST + timedelta(hours=30), FIRST + timedelta(hours=30, minutes=15)]
    assert stats.rows["heart_rate"] == 43 * 60 and stats.rows["stress"] == 1 and stats.rows["spo2"] == 1 and stats.rows["sleep"] == 1 and stats.rows["sleep_stages"] == 4
    # the band's offset (CET) was applied: the first minute is local midnight
    row = conn.execute("SELECT ts_utc, local_date, steps FROM steps WHERE source = ? ORDER BY ts_utc LIMIT 1", (SOURCE,)).fetchone()
    assert (row[0], row[1], row[2]) == ("2026-11-05T23:00:00Z", "2026-11-06", 3)
    last_day = conn.execute("SELECT MAX(steps) FROM steps WHERE source = ? AND local_date = '2026-11-07'", (SOURCE,)).fetchone()[0]
    assert last_day == 19 * 60 * 4
    kinds = {r[0] for r in conn.execute("SELECT kind FROM raw_files WHERE source = ?", (SOURCE,))}
    assert kinds == {"zepp_activity", "zepp_stress", "zepp_spo2", "zepp_hrv", "zepp_resting_hr", "zepp_respiration", "zepp_sleep", "zepp_pai", "zepp_hr_max", "zepp_workout"}
    assert (cfg.raw_dir / SOURCE / "2026" / "zepp_stress").exists()
    synced = all_sync(conn, SOURCE)
    assert synced["activity"] == FIRST + timedelta(minutes=43 * 60 - 1) and synced["stress"] == synced["activity"]
    assert synced["spo2"] == FIRST + timedelta(hours=2) and synced["sleep"] == FIRST + timedelta(hours=7)
    assert synced["pai"] == FIRST + timedelta(hours=1) and synced["hr_max"] == FIRST + timedelta(hours=10)
    assert synced["workout"] == FIRST + timedelta(hours=30, minutes=15)  # the moment after the last workout
    assert conn.execute("SELECT COUNT(*) FROM daily_summary WHERE source = ?", (SOURCE,)).fetchone()[0] == 2
    assert band.acks and all(keep for _, keep in band.acks)  # data stays on the band
    assert band.time_set and band.time_set[0][0] == 5
    assert all(a[0] == 0x04 for a in transport.client_acks)  # we acked the band's chunks
    assert any("activity: 2580 samples" in line for line in logs)


def test_second_pull_continues_where_it_left_off(cfg: Config, band: FakeBandState):
    conn, first, _, _ = run_pull(cfg, band)
    conn.close()
    # the band recorded one more hour; activity is asked from local midnight again, the others from the next minute
    band.minutes["activity"] = (FIRST, band.minutes["activity"][1] + activity_minutes(60, steps=10, hr=80))
    band.minutes["stress"] = (FIRST, band.minutes["stress"][1] + stress_minutes([44] * 60))
    band.fetch_requests.clear()
    conn, second, logs, _ = run_pull(cfg, band)
    asked = dict(band.fetch_requests)
    assert asked["activity"] == datetime(2026, 11, 6, 23, 0, tzinfo=UTC)  # local midnight 2026-11-07
    assert asked["stress"] == FIRST + timedelta(minutes=43 * 60)
    assert asked["spo2"] == FIRST + timedelta(hours=2, minutes=1)
    assert second.files_new == 2 and second.fetched["activity"] == 20 * 60 and second.fetched["spo2"] == 0 and second.fetched["pai"] == 0 and second.fetched["workout"] == 0
    # the day's counter continues instead of restarting at the new buffer's first minute
    last_day = conn.execute("SELECT MAX(steps) FROM steps WHERE source = ? AND local_date = '2026-11-07'", (SOURCE,)).fetchone()[0]
    assert last_day == 19 * 60 * 4 + 60 * 10
    assert table_count(conn, "heart_rate") == 44 * 60
    assert any("nothing new" in line for line in logs)
    # a third pull with nothing new changes nothing
    band.fetch_requests.clear()
    conn, third, _, _ = run_pull(cfg, band)
    # activity is always asked from local midnight: the same bytes come back and are skipped by SHA-256
    assert third.files_new == 0 and third.files_skipped == 1 and third.rows == {} and all(v == 0 for k, v in third.fetched.items() if k != "activity")


def test_extended_night_replaces_the_earlier_report(cfg: Config, band: FakeBandState):
    conn, _, _, _ = run_pull(cfg, band)
    conn.close()
    midnight = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)
    longer = [(1380, 1409, "light"), (1410, 1439, "deep"), (1440, 1449, "light"), (1450, 1479, "rem"), (1480, 1499, "light")]
    band.records["sleep"].append((FIRST + timedelta(hours=9), build_sleep_session_blob(reported=FIRST + timedelta(hours=9), midnight=midnight, stages=longer, score=74, avg_hr=50)))
    conn, stats, _, _ = run_pull(cfg, band)
    nights = conn.execute("SELECT start_utc, end_utc, score FROM sleep WHERE source = ?", (SOURCE,)).fetchall()
    assert [tuple(n) for n in nights] == [("2026-11-05T22:00:00Z", "2026-11-06T00:00:00Z", 74)]
    assert table_count(conn, "sleep_stages") == 5


def test_wrong_key_missing_key_and_absent_band(cfg: Config, band: FakeBandState):
    cfg.strap_authkey_path.write_text("0x" + "ab" * 16)
    with pytest.raises(BandError, match="rejected the auth key"):
        run_pull(cfg, band)
    cfg.strap_authkey_path.unlink()
    with pytest.raises(BandError, match="strap pair"):
        run_pull(cfg, band)
    cfg.strap_authkey_path.write_text(FAKE_KEY.hex())
    with pytest.raises(NoBandError):
        run_pull(cfg, band, present=False)
    cfg.strap_address = ""
    with pytest.raises(BandError, match="no band configured"):
        run_pull(cfg, band)


def test_dry_run_only_counts(cfg: Config, band: FakeBandState):
    conn, stats, logs, _ = run_pull(cfg, band, dry_run=True)
    assert stats.files_new == 0 and stats.rows == {} and stats.fetched["activity"] == 43 * 60 and stats.fetched["spo2"] == 1 and stats.fetched["workout"] > 0
    assert table_count(conn, "raw_files") == 0 and last_sync(conn, SOURCE, "activity") is None
    assert band.acks and all(keep for _, keep in band.acks)
    assert any("activity: 2580 since" in line for line in logs)


def test_keep_false_lets_the_band_delete(cfg: Config, band: FakeBandState):
    cfg.strap_keep_on_band = False
    conn, stats, _, _ = run_pull(cfg, band)
    assert stats.files_new == 11 and band.acks[0] == ("activity", False) and "activity" not in band.minutes


def test_fallback_to_the_control_characteristic(cfg: Config, band: FakeBandState):
    band.fetch_endpoint = False
    conn, stats, _, transport = run_pull(cfg, band)
    assert stats.files_new == 11 and any(uuid.startswith("00000004-") for uuid, _ in transport.writes)


def test_stop_between_kinds(cfg: Config, band: FakeBandState):
    calls = iter([False, True])
    conn, stats, _, _ = run_pull(cfg, band, stop=lambda: next(calls, True))
    assert stats.stopped and stats.files_new == 1


def test_since_for_rules():
    now = NOW
    assert since_for(BY_NAME["stress"], None, now, since_days=10, tz=TZ) == now - timedelta(days=10)
    last = datetime(2026, 11, 7, 10, 30, tzinfo=UTC)
    assert since_for(BY_NAME["stress"], last, now, since_days=10, tz=TZ) == last + timedelta(minutes=1)
    assert since_for(BY_NAME["activity"], last, now, since_days=10, tz=TZ) == datetime(2026, 11, 6, 23, 0, tzinfo=UTC)
    assert since_for(BY_NAME["activity"], None, now, since_days=1, tz=TZ) == datetime(2026, 11, 5, 23, 0, tzinfo=UTC)


def test_read_auth_key_errors(tmp_path: Path):
    with pytest.raises(BandError, match="strap pair"):
        read_auth_key(tmp_path / "missing")
    (tmp_path / "bad").write_text("xyz")
    with pytest.raises(ValueError):
        read_auth_key(tmp_path / "bad")


def test_reparse_and_duplicate_buffer(cfg: Config, band: FakeBandState):
    conn, stats, _, _ = run_pull(cfg, band)
    raw = select_raw_files(conn, source=SOURCE, kind="zepp_stress")
    assert len(raw) == 1
    ok, deleted, counts, sources = reparse_raw_file(conn, cfg, raw[0])
    assert ok and deleted == 1 and counts == {"stress": 1} and sources == [SOURCE]
    # importing the same bytes again is a no-op (SHA-256)
    buf = Buffer(BY_NAME["stress"], FIRST, 4, band.minutes["stress"][1])
    assert import_buffer(conn, cfg, buf, origin="ble://x/stress", fetched_at=NOW) == ("skipped", {})
    # a broken buffer is archived and marked as an error
    bad = Buffer(BY_NAME["spo2"], FIRST, 4, b"\x09" + spo2_record(FIRST, 90))
    state, counts = import_buffer(conn, cfg, bad, origin="ble://x/spo2", fetched_at=NOW)
    assert state == "error" and conn.execute("SELECT parse_status FROM raw_files WHERE original_name LIKE 'spo2_%' ORDER BY id DESC LIMIT 1").fetchone()[0] == "error"
    assert clear_sync(conn, SOURCE) == 10 and all_sync(conn, SOURCE) == {}
