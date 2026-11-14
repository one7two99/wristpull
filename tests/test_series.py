"""Metric registry, daily series, baselines, weekday pattern and the day's curve (series.py)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import build_monitor_fit
from wristpull import series
from wristpull.config import Config
from wristpull.importers.garmin import import_garmin

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 7)


@pytest.fixture
def imported(config: Config, conn, garmin_tree: Path) -> Config:
    for i in range(5):  # five more days before the fixture's 2026-10-06
        start = datetime(2026, 9, 30, 22, 0, tzinfo=UTC) + timedelta(days=i)
        (garmin_tree / "Monitor" / f"M6A2{i:04d}.FIT").write_bytes(build_monitor_fit(start))
    import_garmin(conn, config, path=garmin_tree)
    return config


def test_registry_is_consistent():
    assert len({m.key for m in series.METRICS}) == len(series.METRICS)
    for m in series.METRICS:
        assert m.kind in ("line", "bar", "points") and m.week_agg in ("mean", "sum", "last")
        assert m.compare is None or m.compare in series.BY_KEY
        assert m.samples is None or m.samples in series.SAMPLE_SQL
        assert (m.column is None) == (m.metric is not None)
    assert set(series.TILE_KEYS.values()) <= set(series.BY_KEY)
    assert series.spec("resting_hr").good == "down" and series.spec("nope") is None


def test_rolling_baseline_and_smooth():
    values = [50.0] * 28 + [60.0]
    means, stds = series.rolling_baseline(values)
    assert means[:7] == [None] * 7 and stds[:7] == [None] * 7  # fewer than 7 earlier days: no baseline yet
    assert means[7] == 50.0 and stds[7] == 0.0
    assert means[28] == 50.0 and stds[28] == 0.0  # the day itself is excluded
    spread = [48.0, 52.0] * 14 + [None, 70.0]
    means, stds = series.rolling_baseline(spread)
    assert means[29] == pytest.approx(50.07, abs=0.01) and stds[29] == pytest.approx(2.0, abs=0.1)  # the gap is skipped, not zero
    assert series.rolling_baseline([None] * 10) == ([None] * 10, [None] * 10)
    assert series.smooth([1.0, None, 3.0, 5.0], window=2) == [1.0, None, 3.0, 4.0]
    assert series.z_score(60.0, 50.0, 5.0) == 2.0 and series.z_score(60.0, 50.0, 0.0) is None and series.z_score(None, 50.0, 5.0) is None


def test_stats_and_weekday_pattern():
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(40)]
    values = [50.0 + (i % 7) for i in range(40)]
    values[10] = None
    s = series.stats(days, values)
    assert s["latest"] == 54.0 and s["latest_date"] == "2026-10-10" and s["n"] == 39
    assert s["min"] == 50.0 and s["min_date"] == "2026-09-01" and s["max"] == 56.0 and s["max_date"] == "2026-09-07"
    assert s["mean_7d"] == 53.0 and s["mean_28d"] is not None and s["z"] is not None
    empty = series.stats(days, [None] * 40)
    assert empty["latest"] is None and empty["n"] == 0 and empty["z"] is None
    pattern = series.weekday_pattern(days, values)
    assert [p["weekday"] for p in pattern] == list(range(7)) and sum(p["n"] for p in pattern) == 39
    assert pattern[0]["n"] == 5 and pattern[0]["mean"] == 56.0  # 1 Sep 2026 is a Tuesday: Mondays are i = 6, 13, ... with value 56
    assert series.weekday_pattern(days, [None] * 40)[3] == {"weekday": 3, "mean": None, "n": 0}


def test_weekly_fold():
    days = ["2026-10-05", "2026-10-06", "2026-10-12"]
    assert series.week_labels(["2026-10-07", "2026-10-08", "2026-10-13"]) == ["2026-10-05", "2026-10-12"]
    assert series.fold_weekly(days, [50.0, 52.0, None], "mean") == [51.0, None]
    assert series.fold_weekly(days, [70.0, 68.5, 68.0], "last") == [68.5, 68.0]
    assert series.fold_weekly(days, [10.0, 20.0, 5.0], "sum") == [30.0, 5.0]


def test_metric_series_from_imported_data(imported: Config, conn):
    start = date(2026, 9, 28)
    rhr = series.metric_series(conn, "resting_hr", start, TODAY)
    assert rhr["days"][0] == "2026-09-28" and len(rhr["days"]) == 10 and rhr["strap"] is not None
    assert rhr["garmin"][:3] == [None, None, None] and rhr["garmin"][3] is not None and rhr["garmin"][-1] is None  # 1.10.–6.10. have data
    assert all(v is None for v in rhr["strap"])
    sleep = series.metric_series(conn, "sleep_duration", start, TODAY)
    assert sleep["garmin"][8] == pytest.approx(7.5, abs=0.6)  # the fixture's night, in hours
    steps = series.metric_series(conn, "steps", start, TODAY)
    assert steps["garmin"][8] and steps["garmin"][8] > 1000
    bb = series.metric_series(conn, "body_battery", start, TODAY)
    assert bb["strap"] is None and bb["garmin"][8] is not None
    im = series.metric_series(conn, "intensity_minutes", start, TODAY)
    assert len(im["garmin"]) == 10
    vo2 = series.metric_series(conn, "vo2max", start, TODAY)
    assert vo2["strap"] is None and any(v is not None for v in vo2["garmin"])
    assert series.merged([None, 2.0], [1.0, 9.0]) == [1.0, 2.0] and series.merged([None, 2.0], None) == [None, 2.0]


def test_intraday_curve_and_nights(imported: Config, conn):
    day = date(2026, 10, 6)
    hr = series.intraday(conn, "resting_hr", day, TZ)
    assert hr and hr["day"] == "2026-10-06" and hr["series"][0]["device"] == "garmin"
    points = hr["series"][0]["points"]
    assert points and all(0 <= p[0] < 1440 for p in points) and points == sorted(points)
    assert all(p[0] % series.INTRADAY_STEP_MIN == 0 for p in points) and len(points) <= 288
    steps = series.intraday(conn, "steps", day, TZ)["series"][0]["points"]
    assert [p[1] for p in steps] == sorted(p[1] for p in steps)  # a counter: the maximum per slot keeps it monotone
    assert series.intraday(conn, "sleep_duration", day, TZ) is None  # no sample table
    assert series.intraday(conn, "stress", date(2020, 1, 1), TZ)["series"] == []
    nights = series.night_windows(conn, day, TZ)
    assert nights and all(0 <= a < b <= 1440 for a, b in nights)
    assert series.days_with_samples(conn, "stress", date(2026, 9, 1), TODAY)[-1] == "2026-10-06" and len(series.days_with_samples(conn, "stress", date(2026, 9, 1), TODAY)) == 6
    assert series.days_with_samples(conn, "sleep_duration", date(2026, 9, 1), TODAY) == []


def test_bucket_points():
    assert series.bucket_points([(0, 10), (2, 20), (7, 30)]) == [[0, 15.0], [5, 30.0]]
    assert series.bucket_points([(0, 10), (2, 20)], cumulative=True) == [[0, 20.0]]
    assert series.bucket_points([]) == []


def test_rolling_acwr():
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(35)]
    loads = [100.0] * 35
    acwr = series.rolling_acwr(days, loads)
    assert acwr[:27] == [None] * 27 and acwr[27] == 1.0 and acwr[-1] == 1.0  # a steady load: ratio 1 once 28 days exist
    spike = [100.0] * 28 + [300.0] * 7
    assert series.rolling_acwr(days, spike)[-1] == pytest.approx(300 * 7 / ((100 * 21 + 300 * 7) / 28 * 7), abs=0.01)
    gaps = [None] * 20 + [50.0] * 15
    assert all(v is None for v in series.rolling_acwr(days, gaps))  # fewer than 28 days with a load
    sparse = [100.0 if i % 3 == 0 else None for i in range(35)]  # training every third day, the watch worn every day
    assert series.rolling_acwr(days, sparse)[-1] is None and series.rolling_acwr(days, sparse, present=[True] * 35)[-1] is not None
    assert series.rolling_acwr([], []) == []
