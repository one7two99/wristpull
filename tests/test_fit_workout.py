from datetime import UTC, date, datetime

import pytest

from wristpull.fit.decode import decode_fit
from wristpull.fit.workout import build_schedule_fit, build_workout_fit, decode_custom_hr, hr_fields, local_date_time, scheduled_day, workout_created, workout_serial
from wristpull.workouts.model import HeartRate, Step, Workout, parse_markdown


def _workout() -> Workout:
    return Workout(
        name="Easy run",
        day=date(2026, 11, 5),
        steps=[
            Step("warmup", duration_s=600, hr=HeartRate("bpm", high=130)),
            Step("active", distance_m=5000, hr=HeartRate("bpm", low=135, high=150), note="steady"),
            Step("recovery", duration_s=120, hr=HeartRate("percent", low=60, high=70)),
            Step("active", duration_s=300, hr=HeartRate("zone", zone=4)),
            Step("cooldown", duration_s=300),
            Step("rest"),
        ],
    )


def test_workout_fit_decodes_with_fitdecode():
    fit = decode_fit(build_workout_fit(_workout()))  # check_crc=True by default
    assert fit.file_type == "workout" and fit.manufacturer == "garmin" and fit.time_created == datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    assert fit.product == "connect" and fit.of("file_creator")[0].named["software_version"] == 0  # as Garmin Connect writes them
    header = fit.of("workout")[0].named
    assert header["sport"] == "running" and header["sub_sport"] == "generic" and header["num_valid_steps"] == 6 and header["wkt_name"] == "Easy run"
    assert header["message_index"] == 0 and fit.of("workout")[0].get_raw(5) == 32  # capabilities tcx
    steps = [m.named for m in fit.of("workout_step")]
    assert [s["message_index"] for s in steps] == [0, 1, 2, 3, 4, 5]
    assert steps[0]["duration_type"] == "time" and steps[0]["duration_time"] == 600.0 and steps[0]["intensity"] == "warmup"
    assert steps[0]["target_type"] == "heart_rate" and steps[0]["custom_target_heart_rate_low"] == 130 and steps[0]["custom_target_heart_rate_high"] == 230  # bpm + 100
    assert steps[1]["duration_type"] == "distance" and steps[1]["duration_distance"] == 5000.0
    assert steps[1]["custom_target_heart_rate_low"] == 235 and steps[1]["custom_target_heart_rate_high"] == 250 and steps[1]["notes"] == "steady"
    assert steps[2]["intensity"] == "recovery" and steps[2]["custom_target_heart_rate_low"] == 60 and steps[2]["custom_target_heart_rate_high"] == 70
    assert steps[3]["target_hr_zone"] == 4 and steps[3]["custom_target_heart_rate_low"] == 0
    assert steps[4]["target_type"] == "open" and steps[4]["intensity"] == "cooldown"
    assert steps[5]["duration_type"] == "open" and steps[5]["intensity"] == "rest"
    assert [m.get_raw(0) for m in fit.of("workout_step")][:2] == ["Warm up", "Run"]


def test_other_sports():
    w = _workout()
    w.sport = "strength"
    fit = decode_fit(build_workout_fit(w))
    header = fit.of("workout")[0].named
    assert header["sport"] == "training" and header["sub_sport"] == "strength_training"
    assert fit.of("workout_step")[1].get_raw(0) == "Work"
    w.sport = "cycling"
    assert decode_fit(build_workout_fit(w)).of("workout")[0].named["sport"] == "cycling"


def test_hr_encoding_rules():
    assert hr_fields(None) == (2, 0, 0, 0)
    assert hr_fields(HeartRate("zone", zone=2)) == (1, 2, 0, 0)
    assert hr_fields(HeartRate("bpm", low=135, high=150)) == (1, 0, 235, 250)
    assert hr_fields(HeartRate("bpm", high=130)) == (1, 0, 130, 230)
    assert hr_fields(HeartRate("percent", high=80)) == (1, 0, 1, 80)
    assert decode_custom_hr(250) == ("bpm", 150) and decode_custom_hr(80) == ("percent", 80)


def test_serial_is_stable_and_nonzero():
    w = _workout()
    assert workout_serial(w) == workout_serial(_workout()) and workout_serial(w) != 0
    other = _workout()
    other.name = "Other"
    assert workout_serial(other) != workout_serial(w)
    assert workout_created(Workout(name="x")).year == 2000


def test_invalid_workout_refused():
    w = parse_markdown("# nope\n")
    with pytest.raises(ValueError):
        build_workout_fit(w)


def test_schedule_fit_points_at_the_workout():
    w = _workout()
    fit = decode_fit(build_schedule_fit([w, Workout(name="undated", steps=[Step("active", duration_s=60)])], created=datetime(2026, 11, 2, 8, 0, tzinfo=UTC)))
    assert fit.file_type == "schedules" and fit.time_created == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    rows = fit.of("schedule")
    assert len(rows) == 1  # the undated one is skipped
    row = rows[0].named
    assert row["serial_number"] == workout_serial(w) and row["time_created"] == workout_created(w)
    assert row["type"] == "workout" and row["completed"] is False
    assert rows[0].get_raw(6) == local_date_time(date(2026, 11, 5)) and scheduled_day(rows[0].get_raw(6)) == date(2026, 11, 5)
    assert row["scheduled_time"].hour == 12  # noon, like the watch's own Schedule.fit
    assert fit.product == "connect" and rows[0].named["garmin_product"] == "connect"
