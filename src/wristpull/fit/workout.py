"""Encode a workout (and an optional schedule) as FIT files the watch can import.

Field numbers follow the FIT profile: ``workout`` (26), ``workout_step`` (27), ``schedule`` (28).
Custom heart-rate targets are stored as bpm + 100; values up to 100 mean percent of max HR.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from ..timeutil import FIT_EPOCH, utc_to_fit
from ..workouts.model import HR_BPM_MAX, HR_BPM_MIN, HeartRate, Step, Workout
from .writer import FitWriter

FILE_WORKOUT = 5
FILE_SCHEDULE = 7
MANUFACTURER_GARMIN = 1
PRODUCT_CONNECT = 65534  # garmin_product 'connect': what the workout files written by Garmin Connect carry
CAPABILITIES_TCX = 32
# sport, sub_sport numbers per model sport (sub_sport 0 = generic)
SPORT_NUM: dict[str, tuple[int, int]] = {"running": (1, 0), "cycling": (2, 0), "walking": (11, 0), "strength": (10, 20), "cardio": (10, 26)}
DURATION_TIME, DURATION_DISTANCE, DURATION_OPEN = 0, 1, 5
TARGET_HEART_RATE, TARGET_OPEN = 1, 2
INTENSITY_NUM = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3, "recovery": 4}
STEP_NAMES = {"warmup": "Warm up", "active": "Run", "recovery": "Recover", "cooldown": "Cool down", "rest": "Rest"}
ACTIVE_NAME = {"running": "Run", "cycling": "Ride", "walking": "Walk", "strength": "Work", "cardio": "Work"}
HR_OFFSET = 100


def hr_fields(hr: HeartRate | None) -> tuple[int, int, int, int]:
    """``(target_type, target_value, custom_low, custom_high)`` for a step's heart-rate target."""
    if hr is None:
        return TARGET_OPEN, 0, 0, 0
    if hr.kind == "zone":
        return TARGET_HEART_RATE, int(hr.zone or 0), 0, 0
    if hr.kind == "percent":
        low = hr.low if hr.low is not None else 1
        high = hr.high if hr.high is not None else 100
        return TARGET_HEART_RATE, 0, low, high
    low = (hr.low if hr.low is not None else HR_BPM_MIN) + HR_OFFSET
    high = (hr.high if hr.high is not None else HR_BPM_MAX) + HR_OFFSET
    return TARGET_HEART_RATE, 0, low, high


def decode_custom_hr(value: int) -> tuple[str, int]:
    """Inverse of the bpm + 100 rule: ``('bpm', 150)`` or ``('percent', 80)``."""
    return ("percent", int(value)) if value <= HR_OFFSET else ("bpm", int(value) - HR_OFFSET)


def workout_serial(workout: Workout) -> int:
    """A stable uint32z serial derived from the name and date (never 0, which means 'invalid' in FIT)."""
    digest = hashlib.sha256(f"{workout.day}|{workout.name}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "little") % 0xFFFFFFFE) + 1


def workout_created(workout: Workout) -> datetime:
    """The file_id time_created: the planned day at 12:00 UTC (the key a schedule file refers to)."""
    day = workout.day or date(2000, 1, 1)
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)


def build_workout_fit(workout: Workout, *, created: datetime | None = None) -> bytes:
    if not workout.valid:
        raise ValueError("; ".join(workout.errors))
    created = created or workout_created(workout)
    w = FitWriter()
    sport, sub_sport = SPORT_NUM.get(workout.sport, (0, 0))
    w.message(0, [(0, "enum", FILE_WORKOUT), (1, "uint16", MANUFACTURER_GARMIN), (2, "uint16", PRODUCT_CONNECT), (3, "uint32z", workout_serial(workout)), (4, "uint32", utc_to_fit(created))])
    w.message(49, [(0, "uint16", 0), (1, "uint8", 0)])  # file_creator, as in Connect's files
    w.message(26, [(254, "uint16", 0), (4, "enum", sport), (11, "enum", sub_sport), (5, "uint32z", CAPABILITIES_TCX), (6, "uint16", len(workout.steps)), (8, "string", workout.name)])
    for index, step in enumerate(workout.steps):
        w.message(27, step_fields(index, step, sport=workout.sport))
    return w.finish()


def step_fields(index: int, step: Step, *, sport: str = "running") -> list[tuple[int, str, object]]:
    if step.duration_s is not None:
        duration_type, duration_value = DURATION_TIME, step.duration_s * 1000
    elif step.distance_m is not None:
        duration_type, duration_value = DURATION_DISTANCE, step.distance_m * 100
    else:
        duration_type, duration_value = DURATION_OPEN, 0
    target_type, target_value, low, high = hr_fields(step.hr)
    fields: list[tuple[int, str, object]] = [
        (254, "uint16", index),
        (0, "string", ACTIVE_NAME.get(sport, "Work") if step.intensity == "active" else STEP_NAMES[step.intensity]),
        (1, "enum", duration_type),
        (2, "uint32", duration_value),
        (3, "enum", target_type),
        (4, "uint32", target_value),
        (5, "uint32", low),
        (6, "uint32", high),
        (7, "enum", INTENSITY_NUM[step.intensity]),
    ]
    if step.note:
        fields.append((8, "string", step.note[:50]))
    return fields


def local_date_time(day: date, hour: int = 12) -> int:
    """FIT ``local_date_time``: seconds since the FIT epoch as if the local wall clock were UTC."""
    naive = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)
    return int((naive - FIT_EPOCH).total_seconds())


def build_schedule_fit(workouts: list[Workout], *, created: datetime | None = None, hour: int = 12) -> bytes:
    """One ``schedule`` message per workout, pointing at its file_id (serial + time_created) and day.

    The watch's own ``Schedule/Schedule.fit`` stamps scheduled workouts at 12:00 local, so that is the default.
    """
    created = created or datetime.now(UTC).replace(microsecond=0)
    w = FitWriter()
    w.message(0, [(0, "enum", FILE_SCHEDULE), (1, "uint16", MANUFACTURER_GARMIN), (2, "uint16", PRODUCT_CONNECT), (3, "uint32z", 1), (4, "uint32", utc_to_fit(created))])
    for workout in workouts:
        if workout.day is None:
            continue
        w.message(
            28,
            [
                (0, "uint16", MANUFACTURER_GARMIN),
                (1, "uint16", PRODUCT_CONNECT),
                (2, "uint32z", workout_serial(workout)),
                (3, "uint32", utc_to_fit(workout_created(workout))),
                (4, "enum", 0),
                (5, "enum", 0),
                (6, "uint32", local_date_time(workout.day, hour)),
            ],
        )
    return w.finish()


def scheduled_day(local_seconds: int) -> date:
    return (FIT_EPOCH + timedelta(seconds=int(local_seconds))).date()
