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
    assert target_from_text("easy, Z3", defaults=d) == HeartRate("zone", zone=3)  # an explicit target wins over words
    assert target_from_text("Easy, zone 2", defaults=d) == HeartRate("zone", zone=2)
    assert target_from_text("tempo 150-160 bpm", defaults=d) == HeartRate("bpm", low=150, high=160)
    assert target_from_text("70-80 %", defaults=d) == HeartRate("percent", low=70, high=80)
    assert target_from_text("max 140", defaults=d) == HeartRate("bpm", high=140)
    assert target_from_text("hard intervals", defaults=d) == HeartRate("zone", zone=5)
    assert target_from_text("Schwellenlauf", defaults=d) == HeartRate("zone", zone=4)
    assert target_from_text("moderate", defaults=d) == HeartRate("zone", zone=3)
    assert target_from_text("steady", defaults=d) == HeartRate("zone", zone=3)
    assert target_from_text("steady and relaxed", defaults=d) == HeartRate("zone", zone=2)  # easy words win
    assert target_from_text("locker", defaults=d) == HeartRate("zone", zone=2)
    assert target_from_text("whatever", defaults=d) == HeartRate("zone", zone=2)  # the configured default
    # the intensity column is consulted before the session text
    assert target_from_text("Easy, zone 2", "Outdoor run, steady running with strides", defaults=d) == HeartRate("zone", zone=2)
    assert target_from_text("", "Tempo run 150-160", defaults=d) == HeartRate("bpm", low=150, high=160)


def test_main_part_from_rows():
    d = Defaults(warmup_min=10, cooldown_min=5)
    assert main_part(PlanRow(0, "Easy run", 45, "easy"), d).duration_s == 30 * 60
    assert main_part(PlanRow(0, "Short", 12, "easy"), d).duration_s == 10 * 60  # never below ten minutes
    assert main_part(PlanRow(0, "Tempo run 5 km", None, "tempo"), d).distance_m == 5000
    assert main_part(PlanRow(0, "Strides 400 m", None, ""), d).distance_m == 400
    assert main_part(PlanRow(0, "Run", None, ""), d).duration_s == 30 * 60
    w = workout_from_row(PlanRow(2, "Tempo run 5 km", None, "tempo 150-160 bpm"), date(2026, 11, 5), d)
    assert w.name == "Tempo run 5 km 05.11." and w.description == "tempo 150-160 bpm" and w.valid and len(w.steps) == 3 and w.sport == "running"
    # long plan prose becomes a short name, the full text stays in the description, the sport follows the words
    w = workout_from_row(PlanRow(0, "Outdoor run: 5 min brisk walk, then 20 min easy", 30, "Easy, talk test"), date(2026, 11, 2), d)
    assert w.name == "Outdoor run 02.11." and w.sport == "running" and w.description == "Outdoor run: 5 min brisk walk, then 20 min easy; Easy, talk test"
    assert w.steps[1].hr == HeartRate("zone", zone=2) and w.steps[1].duration_s == 15 * 60
    w = workout_from_row(PlanRow(2, "Home strength: goblet squats, rows, planks", 45, "Moderate"), date(2026, 11, 4), d)
    assert w.name == "Home strength 04.11." and w.sport == "strength" and w.valid
    w = workout_from_row(PlanRow(6, "Ergometer, steady and relaxed", 60, "Easy to moderate"), date(2026, 11, 8), d)
    assert w.name == "Ergometer 08.11." and w.sport == "cycling" and w.steps[1].hr == HeartRate("zone", zone=2)


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
    assert [w.name for w in list_workouts(config)] == ["Easy run 03.11.", "Tempo run 5 km 05.11.", "Mine 06.11.", "Long run 08.11."]
    # a second run creates nothing new; an explicit week start moves the dates
    again = drafts_from_plan(conn, config)
    assert again.created == [] and len(again.skipped) == 5
    moved = drafts_from_plan(conn, config, week_start=date(2026, 11, 10))
    assert [p.name for p in moved.created][0] == "2026-11-10-easy-run.md"


def test_drafts_of_a_week_get_distinct_names(config: Config, conn):
    """Two sessions with the same plan text in one week become two names the watch keeps apart."""
    plan = PLAN.replace("| Sat | Long run | 1:30 h | 70-80 % |", "| Sat | Easy run | 45 min | easy, Z2 |")
    save_run(conn, kind="weekly", period_start="2026-10-27", period_end="2026-11-02", language="en", backend="fake", model="m", prompt_sha256="x", system_prompt="s", user_prompt="u", answer_md=plan)
    drafts_from_plan(conn, config)
    items = list_workouts(config)
    assert [w.name for w in items if w.name.startswith("Easy")] == ["Easy run 03.11.", "Easy run 08.11."]
    assert all(w.valid for w in items)
