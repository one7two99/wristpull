from datetime import date
from pathlib import Path

from gb_db import build_gb_db
from wristpull.coach.context import DataPackage, build_daily, build_weekly, day_rows, render, review_week
from wristpull.importers.garmin import import_garmin
from wristpull.importers.phone import import_gadgetbridge_db

TODAY = date(2026, 10, 6)


def test_build_daily_sections(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)
    import_gadgetbridge_db(conn, config, build_gb_db(tmp_path / "Gadgetbridge.db"))
    pkg = build_daily(conn, config, TODAY, digest="- 2026-10-05 daily: easy run", journal="legs heavy")
    titles = [s.title for s in pkg.sections]
    assert titles[:3] == ["Coverage", "Last night", "Yesterday"]
    assert {"Activities, last 7 days", "Last 7 days", "Trends (latest day vs baselines)", "Training load", "Warning flags", "Athlete's journal", "Previous coaching", "Data caveats"} <= set(titles)
    body = {s.title: s.body for s in pkg.sections}
    assert "Garmin data on 2 of the last 7 days" in body["Coverage"]
    assert body["Last night"].startswith("- garmin: in bed 23:10 to 07:20, slept 7.9 h") and "score 78" in body["Last night"]
    assert "running" in body["Activities, last 7 days"] and "load" in body["Activities, last 7 days"]
    assert body["Last 7 days"].count("\n") == 3  # header, separator, two data rows
    assert "only 2 days of history" in body["Training load"]
    assert "no earlier days to compare with" in body["Trends (latest day vs baselines)"]
    assert "legs heavy" in body["Athlete's journal"] and "easy run" in body["Previous coaching"]
    assert "missing is not zero" in body["Data caveats"] and "Only 2 days with any data" in body["Data caveats"]
    assert pkg.facts["days_with_garmin_7d"] == 2 and pkg.facts["load"]["history_days"] == 2
    rows = day_rows(conn, date(2026, 10, 1), TODAY)
    assert [r["date"] for r in rows] == ["2026-10-05", "2026-10-06"]


def test_build_daily_without_data(conn, config):
    pkg = build_daily(conn, config, TODAY)
    body = {s.title: s.body for s in pkg.sections}
    assert body["Last night"] == "No sleep record for last night." and body["Yesterday"].startswith("No daily summary")
    assert body["Warning flags"] == "- none" and "Previous coaching" not in body
    text, dropped = render(pkg, budget_tokens=6000)
    assert text.startswith("## Warning flags") or text.startswith("## Coverage")
    assert dropped == []


def test_render_trims_low_priority_sections_first():
    pkg = DataPackage("daily", date(2026, 10, 1), date(2026, 10, 6))
    pkg.add("Coverage", "x" * 400, 100)
    pkg.add("Previous coaching", "y" * 4000, 50)
    pkg.add("Data caveats", "z" * 4000, 40)
    text, dropped = render(pkg, budget_tokens=400)
    assert dropped == ["Data caveats", "Previous coaching"]
    assert "## Coverage" in text and "omitted for size: Data caveats, Previous coaching" in text
    text, dropped = render(pkg, budget_tokens=100_000)
    assert dropped == [] and text.index("## Coverage") < text.index("## Previous coaching") < text.index("## Data caveats")


def test_meaningful_activity_filter():
    from wristpull.coach.context import meaningful_activity

    assert not meaningful_activity({"duration_min": 3.0, "distance_km": None, "avg_hr": None})
    assert meaningful_activity({"duration_min": 3.0, "distance_km": None, "avg_hr": 120})
    assert meaningful_activity({"duration_min": 45.0, "distance_km": None, "avg_hr": None})
    assert meaningful_activity({"duration_min": None, "distance_km": 1.2, "avg_hr": None})


def test_review_week_rule():
    assert review_week(date(2026, 11, 1)) == (date(2026, 10, 26), date(2026, 11, 1))  # Sunday: the current week
    assert review_week(date(2026, 11, 2)) == (date(2026, 10, 26), date(2026, 11, 1))  # Monday: the week just finished
    assert review_week(date(2026, 11, 5)) == (date(2026, 10, 26), date(2026, 11, 1))  # Thursday: still the last completed week


def test_build_weekly_sections(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)
    import_gadgetbridge_db(conn, config, build_gb_db(tmp_path / "Gadgetbridge.db"))
    pkg = build_weekly(conn, config, date(2026, 10, 11), digest="- earlier plan")  # Sunday of the week with the fixture data
    body = {s.title: s.body for s in pkg.sections}
    assert pkg.start == date(2026, 10, 5) and pkg.end == date(2026, 10, 11)
    assert "ISO week 41" in body["Coverage"] and "Days with data: 2 (previous week 0)" in body["Coverage"]
    assert body["Week vs previous week"].startswith("- steps per day:") and "training: 1 activities" in body["Week vs previous week"]
    assert body["Days of the week"].count("\n") == 3
    assert "running" in body["Activities"] and "TE " in body["Activities"]
    assert "garmin" in body["Nights"] and "7.9 h" in body["Nights"]
    assert "| week |" in body["Last 6 weeks"] and "2026-W41" in body["Last 6 weeks"]
    assert "only 2 days of history" in body["Training load"]
    assert body["Previous coaching"] == "- earlier plan" and "Only 2 of 7 days" in body["Data caveats"]
    empty = build_weekly(conn, config, date(2026, 12, 6))
    assert {s.title: s.body for s in empty.sections}["Week vs previous week"] == "No daily data in the review week."


def test_meaningful_frame_filters_blips(conn, config, tmp_path: Path):
    import polars as pl

    from wristpull.coach.context import meaningful_frame

    frame = pl.DataFrame({"duration_min": [3.0, 3.0, 45.0, None], "distance_km": [None, None, None, 2.0], "avg_hr": [None, 110, None, None]})
    assert meaningful_frame(frame)["duration_min"].to_list() == [3.0, 45.0, None]
    assert meaningful_frame(frame.clear()).is_empty()
