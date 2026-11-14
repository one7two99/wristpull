"""The insights package: baselines, readiness, sleep regularity, correlations, sweet spot, tags, the package."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from wristpull.insights import baselines, correlate, readiness
from wristpull.insights import sleep as sleep_mod
from wristpull.insights.package import build_insights
from wristpull.insights.store import replace_tags, set_tags, tags_between, tags_for
from wristpull.insights.tags import KEYS, valid

TZ = ZoneInfo("Europe/Berlin")
START = date(2026, 9, 1)


def frame_for(days: int, **metrics: list[float | None]) -> pl.DataFrame:
    """A calendar frame with the given metric columns (lists of length ``days``)."""
    data = {"date": [START + timedelta(days=i) for i in range(days)]}
    for name, values in metrics.items():
        data[name] = pl.Series(values, dtype=pl.Float64)
    return pl.DataFrame(data)


def test_merged_frame_and_calendar():
    daily = pl.DataFrame({"date": [START, START + timedelta(days=2)], "garmin_resting_hr": [50.0, None], "strap_resting_hr": [None, 52.0], "garmin_body_battery_max": [80.0, 70.0]})
    merged = baselines.merged_frame(daily, ("resting_hr", "body_battery_max", "steps"))
    assert merged["resting_hr"].to_list() == [50.0, 52.0] and merged["body_battery_max"].to_list() == [80.0, 70.0] and merged["steps"].to_list() == [None, None]
    cal = baselines.calendar_frame(merged, START, START + timedelta(days=3))
    assert cal.height == 4 and cal["resting_hr"].to_list() == [50.0, None, 52.0, None]


def test_rolling_z_known_values_and_gaps():
    values = [50.0] * 28 + [60.0]
    frame = baselines.rolling_baselines(frame_for(29, resting_hr=values), ("resting_hr",))
    z = frame["resting_hr_z"].to_list()
    assert z[:8] == [None] * 8 and z[28] is None  # a constant window has no spread: no z
    spread = [48.0, 52.0] * 14 + [56.0]
    frame = baselines.rolling_baselines(frame_for(29, resting_hr=spread), ("resting_hr",))
    assert frame["resting_hr_mean28"][28] == pytest.approx(50.0) and frame["resting_hr_z"][28] == pytest.approx((56 - 50) / frame["resting_hr_std28"][28])
    assert frame["resting_hr_mean7"][7] == pytest.approx(348 / 7) and frame["resting_hr_median28"][28] == pytest.approx(50.0)  # the 7 days before: 4 x 48, 3 x 52
    gappy = [48.0, 52.0] * 5 + [None] * 10 + [50.0] * 9
    frame = baselines.rolling_baselines(frame_for(29, resting_hr=gappy), ("resting_hr",))
    assert frame["resting_hr_mean28"][20] == pytest.approx(50.0) and frame["resting_hr_mean28"][5] is None  # gaps skipped; fewer than 7 earlier values: None
    means, stds = baselines.rolling_stats([None] * 3)
    assert means == [None] * 3 and stds == [None] * 3 and baselines.rolling_stats([]) == ([], [])
    rows = baselines.rows_of(frame)
    assert rows[0]["date"] == "2026-09-01"


def _row(**values):
    row = {"date": "2026-10-01"}
    for key, column, _d, _w in readiness.COMPONENTS:
        if key == "acwr":
            row["acwr"] = values.get("acwr")
            continue
        row[column] = values.get(key, 0.0)
        row[f"{column}_z"] = values.get(f"{key}_z")
    row["sleep_duration_s"] = values.get("sleep_s", 7.5 * 3600)
    return row


def test_readiness_monotone_partial_and_floor():
    neutral = _row(hrv_z=0.0, resting_hr_z=0.0, sleep_duration_z=0.0, sleep_score_z=0.0, body_battery_z=0.0, acwr=1.0)
    previous = _row(stress_z=0.0)
    r0 = readiness.readiness_for(neutral, previous)
    assert r0["score"] == pytest.approx(52.5, abs=0.1) and r0["label"] == "moderate" and r0["confidence"] == 1.0  # all neutral except the free ACWR
    assert sum(c["contribution"] for c in r0["components"]) == pytest.approx(r0["score"] - 50, abs=0.2)
    better = readiness.readiness_for(_row(hrv_z=1.0, resting_hr_z=-1.0, sleep_duration_z=0.0, sleep_score_z=0.0, body_battery_z=0.0, acwr=1.0), previous)
    assert better["score"] == pytest.approx(61.5, abs=0.1) and better["label"] == "moderate"  # hrv and rhr one z better: 70 each
    worse = readiness.readiness_for(_row(hrv_z=-2.0, resting_hr_z=2.0, sleep_duration_z=-1.0, sleep_score_z=-1.0, body_battery_z=-1.0, acwr=1.8), _row(stress_z=2.0))
    assert worse["score"] < 45 and worse["label"] == "recover" and next(c for c in worse["components"] if c["key"] == "acwr")["score"] == 0.0
    extreme = readiness.readiness_for(_row(hrv_z=9.0, resting_hr_z=0.0, sleep_duration_z=0.0, sleep_score_z=0.0, body_battery_z=0.0, acwr=1.0), previous)
    assert next(c for c in extreme["components"] if c["key"] == "hrv")["score"] == 100.0  # clamped at 2.5 z
    floor = readiness.readiness_for(_row(hrv_z=0.0, resting_hr_z=0.0, sleep_duration_z=1.0, sleep_s=5 * 3600, sleep_score_z=0.0, body_battery_z=0.0, acwr=1.0), previous)
    assert next(c for c in floor["components"] if c["key"] == "sleep_duration")["score"] == 30.0  # under 6 h: capped
    partial = readiness.readiness_for(_row(hrv_z=1.0, resting_hr_z=0.0, sleep_duration_z=None, sleep_score_z=None, body_battery_z=None, acwr=None), None)
    assert partial["score"] is not None and partial["confidence"] == pytest.approx(0.45) and not next(c for c in partial["components"] if c["key"] == "stress")["present"]
    one = readiness.readiness_for(_row(hrv_z=1.0, resting_hr_z=None, sleep_duration_z=None, sleep_score_z=None, body_battery_z=None, acwr=None), None)
    assert one["score"] is None and one["label"] is None
    assert readiness.label(70) == "ready" and readiness.label(44.9) == "recover" and readiness.label(None) is None
    series = readiness.readiness_series([previous, neutral])
    assert series[1]["score"] == r0["score"] and series[0]["components"][5]["present"] is False  # the first day has no "yesterday"


def test_sleep_consistency_and_bed_offset():
    assert sleep_mod.bed_offset(23 * 60 + 30) == 330 and sleep_mod.bed_offset(30) == 390 and sleep_mod.bed_offset(18 * 60) == 0
    assert sleep_mod.clock_minutes("2026-10-05T21:30:00Z", "Europe/Berlin", TZ) == 23 * 60 + 30 and sleep_mod.clock_minutes("2026-10-05T21:30:00Z", "Nowhere/Invalid", TZ) == 23 * 60 + 30
    assert sleep_mod.clock_minutes("2026-11-05T21:30:00Z", "Europe/Berlin", TZ) == 22 * 60 + 30  # after the DST change the clock time differs
    nights = [{"date": (date(2026, 10, 1) + timedelta(days=i)).isoformat(), "bed_offset": off, "wake_min": 7 * 60, "sleep_h": h}
              for i, (off, h) in enumerate([(300, 8.0), (330, 7.5), (360, 7.0), (330, 6.0)])]
    c = sleep_mod.sleep_consistency(nights, date(2026, 10, 4), window_days=14, target_h=7.5)
    assert c["n_nights"] == 4 and c["nights_missing"] == 10 and c["bedtime_std_min"] == 21 and c["wake_std_min"] == 0
    assert c["bedtime_mean"] == "23:30" and c["wake_mean"] == "07:00" and c["mean_h"] == 7.12 and c["debt_h"] == 2.0 and c["balance_h"] == -1.5
    empty = sleep_mod.sleep_consistency(nights, date(2020, 1, 1))
    assert empty["n_nights"] == 0 and empty["debt_h"] is None


def _rows_with(x: str, y: str, xs: list[float], ys: list[float]) -> list[dict]:
    rows = []
    for i, (xv, yv) in enumerate(zip(xs, ys)):
        rows.append({"date": (START + timedelta(days=i)).isoformat(), x: xv, f"{x}_z": xv, y: yv, f"{y}_z": yv})
    return rows


def test_correlation_sign_lag_min_n_and_sweet_spot():
    xs = [float(i % 10) for i in range(40)]
    ys_next = [0.0] + [2 * v for v in xs[:-1]]  # y on day d+1 = 2 * x on day d
    rows = _rows_with("sleep_duration_s", "resting_hr", [v * 3600 for v in xs], ys_next)
    for row, xv in zip(rows, xs):
        row["sleep_duration_s_z"] = xv
    lag1 = correlate.correlation(rows, "sleep_duration_s", "resting_hr", 1)
    assert lag1["r"] == 1.0 and lag1["label"] == "strong" and lag1["direction"] == "+" and lag1["significant"] and lag1["n"] == 39
    assert lag1["by_lag"]["1"] == 1.0 and lag1["by_lag"]["0"] < 1.0
    negative = correlate.correlation(_rows_with("stress_avg", "sleep_score", xs, [-v for v in xs]), "stress_avg", "sleep_score", 0)
    assert negative["r"] == -1.0 and negative["direction"] == "-"
    few = correlate.correlation(_rows_with("stress_avg", "sleep_score", xs[:15], xs[:15]), "stress_avg", "sleep_score", 0)
    assert few["r"] is None and few["label"] == "insufficient" and few["bins"] == [] and few["sweet_spot"] is None
    assert correlate.strength(0.05, 30)["label"] == "none" and correlate.strength(0.2, 30)["label"] == "weak" and correlate.strength(0.4, 30)["label"] == "moderate"
    assert correlate.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0 and correlate.spearman([1, 1, 1], [1, 2, 3]) is None and correlate._ranks([10, 20, 10]) == [1.5, 3.0, 1.5]
    # peaked response: sleep 7–7.5 h gives the best HRV, flat elsewhere
    hours = [5.5, 6.2, 6.7, 7.2, 7.7, 8.5] * 10
    hrv = [40.0 if not 7.0 <= h < 7.5 else 55.0 for h in hours]
    rows = _rows_with("sleep_duration_s", "hrv_last_night_avg_ms", [h * 3600 for h in hours], hrv)
    for row, h in zip(rows, hours):
        row["sleep_duration_s_z"] = h
    peaked = correlate.correlation(rows, "sleep_duration_s", "hrv_last_night_avg_ms", 0)
    assert [b["label"] for b in peaked["bins"]] == ["< 6", "6–6.5", "6.5–7", "7–7.5", "7.5–8", "≥ 8"] and all(b["n"] == 10 for b in peaked["bins"])
    assert peaked["sweet_spot"]["bin"] == "7–7.5" and peaked["sweet_spot"]["mean_y"] == 55.0
    flat = correlate.correlation(_rows_with("sleep_duration_s", "hrv_last_night_avg_ms", [h * 3600 for h in hours], [40.0] * 60), "sleep_duration_s", "hrv_last_night_avg_ms", 0)
    assert flat["sweet_spot"] is None  # no bin beats its neighbours
    assert correlate.sweet_spot([{"n": 2, "mean_y_z": 1.0}, {"n": 2, "mean_y_z": 0.0}], 1) is None  # too few days per bin
    assert correlate.bin_label("bed_offset", 300.0, 360.0) == "23:00–00:00" and correlate.bin_label("sleep_duration_s", None, 6.0) == "< 6"
    assert correlate.edges_for("bed_offset", [290.0, 400.0]) == [300.0, 360.0] and correlate.edges_for("stress_avg", [1.0, 2.0]) == []


def test_tag_effects():
    rows = []
    for i in range(40):
        day = (START + timedelta(days=i)).isoformat()
        rows.append({"date": day, "hrv_last_night_avg_ms": 40.0, "hrv_last_night_avg_ms_z": 0.0, "resting_hr": 50.0, "resting_hr_z": 0.0, "sleep_score": 80.0, "sleep_score_z": 0.0, "body_battery_max": 80.0, "body_battery_max_z": 0.0})
    tagged = {(START + timedelta(days=i)).isoformat(): {"alcohol"} for i in range(0, 20, 3)}
    for day in tagged:  # the night after: lower HRV
        nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        row = next(r for r in rows if r["date"] == nxt)
        row["hrv_last_night_avg_ms"], row["hrv_last_night_avg_ms_z"] = 34.0, -1.0
    effects = correlate.tag_effects(rows, tagged)
    assert effects[0]["tag"] == "alcohol" and effects[0]["n"] == 7
    hrv = next(e for e in effects[0]["effects"] if e["metric"] == "hrv_last_night_avg_ms")
    assert hrv["delta_z"] == -1.0 and hrv["delta"] == -6.0 and hrv["better"] == 1
    assert correlate.tag_effects(rows, {"2026-09-01": {"sauna"}}) == [{"tag": "sauna", "n": 1, "effects": []}]


def test_tags_store_and_registry(conn):
    assert valid("sauna") and not valid("beer") and len(KEYS) == len(set(KEYS))
    assert set_tags(conn, "2026-10-05", add=["sauna", "alcohol", "beer"]) == ["alcohol", "sauna"]
    assert set_tags(conn, "2026-10-05", remove=["alcohol"]) == ["sauna"] and tags_for(conn, "2026-10-05") == ["sauna"]
    assert replace_tags(conn, "2026-10-05", ["nap", "race"]) == ["nap", "race"]
    assert tags_between(conn, "2026-10-01", "2026-10-31") == {"2026-10-05": {"nap", "race"}} and tags_between(conn, "2020-01-01", "2020-01-02") == {}


def test_build_insights_package(config, conn, garmin_tree):
    from datetime import UTC, datetime

    from conftest import build_monitor_fit
    from wristpull.importers.garmin import import_garmin

    for i in range(5):
        (garmin_tree / "Monitor" / f"M6A2{i:04d}.FIT").write_bytes(build_monitor_fit(datetime(2026, 9, 30, 22, 0, tzinfo=UTC) + timedelta(days=i)))
    import_garmin(conn, config, path=garmin_tree)
    set_tags(conn, "2026-10-03", add=["sauna"])
    pkg = build_insights(conn, config, date(2026, 10, 7), period_days=28)
    assert pkg["period"] == {"start": "2026-09-10", "end": "2026-10-07", "days": 28, "warmup_days": 28, "days_with_data": 7}
    assert len(pkg["readiness"]["series"]["dates"]) == 28 and pkg["readiness"]["latest"] is None  # six days: no baseline yet
    assert pkg["baselines"]["resting_hr"]["latest"] is not None and pkg["baselines"]["resting_hr"]["z"] is None
    assert len(pkg["load"]["acwr"]) == 28 and all(v is None for v in pkg["load"]["acwr"])
    assert pkg["sleep"]["n_nights"] == 1 and pkg["sleep"]["target_h"] == 7.5 and pkg["sleep"]["nights"][0]["date"] == "2026-10-06"
    assert all(c["label"] == "insufficient" for c in pkg["correlations"]) and [c["x"] for c in pkg["correlations"]][0] == "sleep_duration_s"
    assert pkg["tags"] == [{"tag": "sauna", "n": 1, "effects": []}] and pkg["tag_days"] == {"2026-10-03": ["sauna"]}
    assert len(pkg["weekday"]["steps"]) == 7 and any(c.startswith("21 of 28 days") for c in pkg["caveats"])
    import json

    json.dumps(pkg)  # everything is plain JSON
