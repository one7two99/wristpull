from datetime import date
from zoneinfo import ZoneInfo

from wristpull.coach.plan import PlanRow, adherence, parse_duration, parse_plan_table, plan_to_ics

TZ = ZoneInfo("Europe/Berlin")
PLAN = """## Plan for next week

| Day | Session | Duration | Intensity |
|---|---|---|---|
| Mon | Easy run | 40 min | zone 2 |
| Tue | Strength at home | 30 min | moderate |
| Wed | Rest | – | – |
| Thu | Intervals 6×400 m | 45 min | hard |
| Sat | Rest day | | |
| Sun | Long run | 1 h 15 min | easy |
"""


def test_parse_plan_table_and_durations():
    rows = parse_plan_table(PLAN)
    assert [r.day for r in rows] == [0, 1, 2, 3, 5, 6]
    assert rows[0].duration_min == 40 and rows[0].intensity == "zone 2" and not rows[0].is_rest
    assert rows[2].is_rest and rows[2].duration_min is None
    assert rows[5].duration_min == 75 and rows[5].session == "Long run"
    assert parse_duration("1,5 h") == 90 and parse_duration("no numbers") is None and parse_duration("90 minutes") == 90
    german = "| Montag | Lockerer Lauf | 40 min | Zone 2 |\n| So | Ruhetag | | |"
    de = parse_plan_table(german)
    assert [r.day for r in de] == [0, 6] and de[1].is_rest
    assert parse_plan_table("no table here") == []


def test_adherence_lines():
    rows = parse_plan_table(PLAN)
    activities = [
        {"local_date": "2026-10-05", "sport": "running", "duration_min": 32.0, "avg_hr": 148},
        {"local_date": "2026-10-11", "sport": "running", "timer_s": 4500, "avg_hr": None},
        {"local_date": "2026-10-07", "sport": "walking", "duration_min": 20.0},
    ]
    lines = adherence(rows, date(2026, 10, 5), activities, TZ)
    assert lines[0] == "- Mon 2026-10-05: planned Easy run 40 min, zone 2 → running 32 min, avg HR 148 ✓"
    assert "Tue 2026-10-06: planned Strength at home 30 min, moderate → nothing recorded ✗" in lines[1]
    assert lines[2].endswith("→ walking 20 min ✗")  # a rest day with training is flagged
    assert lines[4].endswith("→ rest ✓")
    assert "running 75 min ✓" in lines[5]


def test_plan_to_ics():
    rows = parse_plan_table(PLAN)
    ics = plan_to_ics(rows, date(2026, 10, 5), TZ, hour=7)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.count("BEGIN:VEVENT") == 6
    assert "DTSTART;TZID=Europe/Berlin:20261005T070000" in ics and "DTEND;TZID=Europe/Berlin:20261005T074000" in ics
    assert "DTSTART;VALUE=DATE:20261007" in ics  # rest day as all-day event
    assert "SUMMARY:wristpull: Intervals 6×400 m (45 min)" in ics and "DESCRIPTION:hard" in ics
    assert "SUMMARY:wristpull: Long run (75 min)" in ics
    assert plan_to_ics([PlanRow(0, "a; b, c", 10, "")], date(2026, 10, 5), TZ).count("a\; b\\, c") == 1
