from wristpull.coach import metrics


def rows_for(days: int, *, load: float = 50.0, rhr: int = 52, hrv: float = 40.0) -> list[dict]:
    out = []
    for i in range(days):
        out.append({"date": f"2026-10-{i + 1:02d}", "garmin_training_load": load, "garmin_resting_hr": rhr, "garmin_hrv_last_night_avg_ms": hrv, "garmin_sleep_duration_s": 7 * 3600, "garmin_sleep_score": 80, "garmin_body_battery_max": 85, "strap_steps": 5000})
    return out


def test_value_falls_back_to_strap_and_baselines():
    rows = rows_for(10)
    rows[-1]["garmin_resting_hr"] = None
    rows[-1]["strap_resting_hr"] = 55
    assert metrics.value(rows[-1], "resting_hr") == 55.0
    assert metrics.value(rows[-1], "steps") == 5000.0
    assert metrics.value({}, "steps") is None
    assert metrics.baseline(rows, "resting_hr", 7, before=rows[-1]["date"]) == 52.0
    assert metrics.mean([None, None]) is None


def test_acute_chronic_and_monotony():
    rows = rows_for(28)
    for r in rows[-7:]:
        r["garmin_training_load"] = 100.0
    load = metrics.acute_chronic(rows)
    assert load["acute_load"] == 700 and load["sufficient_history"]
    # chronic: 21 days at 50 + 7 days at 100 = 1750 / 28 * 7 = 437.5
    assert load["chronic_weekly_load"] == 438 and load["acwr"] == 1.6
    assert load["monotony"] is None  # identical daily loads: zero spread
    rows[-1]["garmin_training_load"] = 150.0
    assert metrics.acute_chronic(rows)["monotony"] is not None
    short = metrics.acute_chronic(rows_for(5))
    assert not short["sufficient_history"] and short["history_days"] == 5
    assert metrics.acute_chronic([])["acwr"] is None


def test_deltas_and_flags():
    rows = rows_for(30)
    rows[-1].update(garmin_resting_hr=58, garmin_hrv_last_night_avg_ms=30.0, garmin_sleep_duration_s=5 * 3600, garmin_sleep_score=55, garmin_body_battery_max=40)
    d = metrics.deltas(rows, "resting_hr")
    assert d["latest"] == 58 and d["mean_28d"] == 52.0 and d["delta_28d"] == 6.0
    assert d["n_7d"] == 7 and d["n_28d"] == 28
    assert metrics.deltas(rows[:1], "resting_hr")["n_7d"] == 0
    flags = metrics.recovery_flags(rows)
    assert any("HRV 30 ms" in f for f in flags)
    assert any("resting HR 58 bpm is +6 bpm" in f for f in flags)
    assert any("short sleep: 5.0 h" in f for f in flags) and any("low sleep score: 55" in f for f in flags)
    assert any("body battery" in f for f in flags)
    for r in rows[-7:]:
        r["garmin_training_load"] = 200.0
    assert any("acute:chronic" in f for f in metrics.recovery_flags(rows))
    assert metrics.recovery_flags(rows_for(10)) == []
    assert metrics.recovery_flags([]) == []
    assert metrics.deltas([], "steps")["latest"] is None
