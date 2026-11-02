from datetime import date

from wristpull.coach.plan import PlanRow
from wristpull.coach.store import save_run
from wristpull.config import Config
from wristpull.workouts.drafts import drafts_from_plan, main_part, target_from_text, workout_from_row
from wristpull.workouts.model import Defaults, HeartRate
from wristpull.workouts.store import list_workouts, save

PLAN = """## Week in review
fine
## Plan for next week
| Day | Session | Duration | Intensity |
|---|---|---|---|
| Mon | Easy run | 45 min | easy, Z2 |
| Tue | Rest | | |
| Wed | Tempo run 5 km | | tempo 150-160 bpm |
| Thu | Intervals | 50 min | hard |
| Sat | Long run | 1:30 h | 70-80 % |
## Numbers used
1
"""


def _store_plan(conn) -> None:
    save_run(conn, kind="weekly", period_start="2026-10-27", period_end="2026-11-02", language="en", backend="fake", model="m", prompt_sha256="x", system_prompt="s", user_prompt="u", answer_md=PLAN)


def test_target_from_text():
    d = Defaults()
    assert target_from_text("easy, Z3", d) == HeartRate("zone", zone=3)  # an explicit target wins over words
    assert target_from_text("tempo 150-160 bpm", d) == HeartRate("bpm", low=150, high=160)
    assert target_from_text("70-80 %", d) == HeartRate("percent", low=70, high=80)
    assert target_from_text("max 140", d) == HeartRate("bpm", high=140)
    assert target_from_text("hard intervals", d) == HeartRate("zone", zone=5)
    assert target_from_text("Schwellenlauf", d) == HeartRate("zone", zone=4)
    assert target_from_text("moderate", d) == HeartRate("zone", zone=3)
    assert target_from_text("locker", d) == HeartRate("zone", zone=2)
    assert target_from_text("whatever", d) == HeartRate("zone", zone=2)  # the configured default


def test_main_part_from_rows():
    d = Defaults(warmup_min=10, cooldown_min=5)
    assert main_part(PlanRow(0, "Easy run", 45, "easy"), d).duration_s == 30 * 60
    assert main_part(PlanRow(0, "Short", 12, "easy"), d).duration_s == 10 * 60  # never below ten minutes
    assert main_part(PlanRow(0, "Tempo run 5 km", None, "tempo"), d).distance_m == 5000
    assert main_part(PlanRow(0, "Strides 400 m", None, ""), d).distance_m == 400
    assert main_part(PlanRow(0, "Run", None, ""), d).duration_s == 30 * 60
    w = workout_from_row(PlanRow(2, "Tempo run 5 km", None, "tempo 150-160 bpm"), date(2026, 11, 5), d)
    assert w.name == "Tempo run 5 km" and w.description == "tempo 150-160 bpm" and w.valid and len(w.steps) == 3


def test_drafts_from_plan(config: Config, conn):
    result = drafts_from_plan(conn, config)
    assert result.message and result.created == []
    _store_plan(conn)
    save(config, workout_from_row(PlanRow(3, "Mine", 40, ""), date(2026, 11, 6), Defaults()))  # Thursday exists already
    result = drafts_from_plan(conn, config)
    assert result.week_start == date(2026, 11, 3) and result.message is None
    assert [p.name for p in result.created] == ["2026-11-03-easy-run.md", "2026-11-05-tempo-run-5-km.md", "2026-11-08-long-run.md"]
    assert result.skipped == ["2026-11-04: rest day", "2026-11-06: a workout file exists already"]
    items = {w.day: w for w in list_workouts(config)}
    assert items[date(2026, 11, 3)].steps[1].hr == HeartRate("zone", zone=2) and items[date(2026, 11, 3)].steps[1].duration_s == 30 * 60
    assert items[date(2026, 11, 5)].steps[1].distance_m == 5000 and items[date(2026, 11, 5)].steps[1].hr == HeartRate("bpm", low=150, high=160)
    assert items[date(2026, 11, 8)].steps[1].duration_s == 75 * 60 and items[date(2026, 11, 8)].steps[1].hr == HeartRate("percent", low=70, high=80)
    assert all(w.valid and w.status == "draft" for w in items.values())
    # a second run creates nothing new; an explicit week start moves the dates
    again = drafts_from_plan(conn, config)
    assert again.created == [] and len(again.skipped) == 5
    moved = drafts_from_plan(conn, config, week_start=date(2026, 11, 10))
    assert [p.name for p in moved.created][0] == "2026-11-10-easy-run.md"
