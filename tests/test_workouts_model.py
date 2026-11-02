from datetime import date

from wristpull.workouts.model import Defaults, HeartRate, Step, Workout, describe_steps, file_name, guess_sport, parse_duration, parse_markdown, parse_step, parse_target, render_markdown, short_name, slug, template

TIME_WORKOUT = """---
date: 2026-11-05
sport: running
name: Easy run
status: draft
---
# Easy run
- warmup 10 min hr <= 130
- run 40 min hr 135-150
- cooldown 5 min hr <= 125
"""

DISTANCE_WORKOUT = """---
date: 2026-11-06
name: Tempo 5k
---
- aufwärmen 10 min hf <= 130
- laufen 5000 m hr 140-155  # hold the pace
- auslaufen 5 min hr max 125
"""


def test_parse_time_workout():
    w = parse_markdown(TIME_WORKOUT)
    assert w.valid and w.day == date(2026, 11, 5) and w.name == "Easy run" and w.sport == "running" and w.status == "draft"
    assert [s.intensity for s in w.steps] == ["warmup", "active", "cooldown"]
    assert [s.duration_s for s in w.steps] == [600, 2400, 300]
    assert w.steps[0].hr == HeartRate("bpm", high=130) and w.steps[1].hr == HeartRate("bpm", low=135, high=150)
    assert w.total_seconds == 3300 and w.total_metres == 0
    assert w.steps[1].kind == "time"


def test_parse_distance_workout_in_german():
    w = parse_markdown(DISTANCE_WORKOUT)
    assert w.valid, w.errors
    main = w.steps[1]
    assert main.kind == "distance" and main.distance_m == 5000 and main.duration_s is None and main.note == "hold the pace"
    assert w.steps[2].hr == HeartRate("bpm", high=125)
    assert w.name == "Tempo 5k" and w.total_metres == 5000


def test_targets_zone_percent_bounds():
    assert parse_target("Z2") == (HeartRate("zone", zone=2), None)
    assert parse_target("zone 4") == (HeartRate("zone", zone=4), None)
    assert parse_target("70-80 %") == (HeartRate("percent", low=70, high=80), None)
    assert parse_target(">= 120") == (HeartRate("bpm", low=120), None)
    assert parse_target("150-135")[1].startswith("heart-rate range")
    assert "outside" in parse_target("240-250")[1]
    assert "outside" in parse_target("50-120 %")[1]
    assert "unreadable" in parse_target("fast")[1]
    assert HeartRate("bpm", high=130).text() == "<= 130" and HeartRate("percent", low=70, high=80).text() == "70-80 %" and HeartRate("zone", zone=3).text() == "Z3"


def test_durations():
    assert parse_duration("40 min") == (2400, None, None)
    assert parse_duration("1:15 h") == (4500, None, None)
    assert parse_duration("1.5 h") == (5400, None, None)
    assert parse_duration("5 km") == (None, 5000, None)
    assert parse_duration("800 m") == (None, 800, None)
    assert parse_duration("open") == (None, None, None)
    assert parse_duration("5 furlongs")[2] is not None


def test_step_errors():
    assert parse_step("- sprint 10 min")[1].startswith("unknown intensity")
    assert parse_step("- run 0 min")[1].endswith("has no length")
    assert parse_step("- run 10 min hr fast")[1].startswith("unreadable heart-rate")
    assert parse_step("- run ten minutes")[1].startswith("unreadable step")
    step, error = parse_step("- run open")
    assert error is None and step.kind == "open" and step.duration_text() == "open"


def test_file_level_errors():
    w = parse_markdown("# No date\n- run 10 min\n")
    assert "missing 'date:'" in w.errors[0]
    w = parse_markdown("---\ndate: 2026-13-40\nsport: curling\nstatus: weird\n---\n")
    assert any("invalid date" in e for e in w.errors) and any("unsupported sport" in e for e in w.errors)
    assert any("unknown status" in e for e in w.errors) and any("no steps" in e for e in w.errors) and any("missing name" in e for e in w.errors)
    w = parse_markdown("---\ndate: 2026-11-05\nname: " + "x" * 31 + "\n---\n- run 10 min\n")
    assert any("longer than 30" in e for e in w.errors)


def test_render_roundtrip_and_names():
    w = parse_markdown(DISTANCE_WORKOUT)
    text = render_markdown(w)
    again = parse_markdown(text)
    assert again.steps == w.steps and again.name == w.name and again.day == w.day
    assert "- run 5 km hr 140-155  # hold the pace" in text and "status: draft" in text
    assert slug("Tempo 5k – Läufe!") == "tempo-5k-laeufe" and file_name(w) == "2026-11-06-tempo-5k.md"
    assert describe_steps(w) == ["1 warmup 10:00  HR <= 130", "2 active 5000 m  HR 140-155  (hold the pace)", "3 cooldown 5:00  HR <= 125"]


def test_templates_follow_defaults():
    d = Defaults(warmup_min=8, cooldown_min=4, warmup_hr_max=128, cooldown_hr_max=120, default_target="70-80 %")
    t = template("time", date(2026, 11, 7), d, main_minutes=30)
    assert t.name == "Easy run 30 min" and [s.duration_s for s in t.steps] == [480, 1800, 240]
    assert t.steps[0].hr == HeartRate("bpm", high=128) and t.steps[1].hr == HeartRate("percent", low=70, high=80)
    r = template("distance", date(2026, 11, 7), d, main_metres=10000, name="Long one")
    assert r.name == "Long one" and r.steps[1].distance_m == 10000 and r.steps[2].hr == HeartRate("bpm", high=120)
    assert parse_markdown(render_markdown(r)).valid
    assert Workout(name="x", steps=[Step("active", duration_s=60)]).valid


def test_short_names_and_sport_guess():
    assert short_name("Outdoor run: 5 min brisk walk, then 20 min easy") == "Outdoor run"
    assert short_name("Home strength (goblet squats)") == "Home strength"
    assert short_name("A very long session title without any punctuation at all") == "A very long session title"
    assert short_name("Long run") == "Long run" and short_name("x" * 40) == "x" * 30
    assert guess_sport("Outdoor run") == "running" and guess_sport("Dauerlauf") == "running"
    assert guess_sport("Home strength") == "strength" and guess_sport("Kraft zu Hause") == "strength"
    assert guess_sport("Ergometer") == "cycling" and guess_sport("Brisk walk") == "walking" and guess_sport("Rowing") == "cardio"
    assert guess_sport("Something else") == "running"
    assert parse_markdown("---\ndate: 2026-11-04\nname: Core\nsport: strength\n---\n- run 20 min\n").valid
