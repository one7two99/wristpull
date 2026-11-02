"""Draft workouts from the coach's last weekly plan (one Markdown file per planned session)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..coach.plan import PlanRow, parse_plan_table
from ..coach.store import last_weekly_plan
from ..config import Config
from .model import Defaults, HeartRate, Step, Workout, guess_sport, parse_target, short_name
from .store import existing_for_day, save

# Checked in this order: an "easy" word wins over a "tempo" word in the same text ("steady, easy" is an easy run).
ZONE_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("easy", "locker", "ruhig", "relaxed", "entspannt", "ga1", "regeneration", "recovery", "erholung", "long run", "dauerlauf", "talk test"), "Z2"),
    (("interval", "intervall", "hard", "hart", "vo2", "5k pace", "wettkampf"), "Z5"),
    (("tempo", "threshold", "schwelle", "schwellen", "zügig", "zugig", "brisk"), "Z4"),
    (("moderate", "moderat", "ga2", "medium", "mittel", "steady"), "Z3"),
)
DISTANCE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(km|m)\b", re.I)
TARGET_IN_TEXT = re.compile(r"\b(Z(?:one)?\s?[1-5]|\d{2,3}\s*[-–]\s*\d{2,3}\s*(?:bpm|%)?|(?:<=|max\.?)\s*\d{2,3})", re.I)


@dataclass
class DraftResult:
    week_start: date | None = None
    created: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "date: reason"
    message: str | None = None


def target_from_text(*texts: str, defaults: Defaults) -> HeartRate | None:
    """A heart-rate target named in the plan (``Z3``, ``zone 2``, ``140-150``, ``70-80 %``) or guessed from
    intensity words. Texts are tried in order, so pass the intensity column before the session text."""
    for text in texts:
        m = TARGET_IN_TEXT.search(text)
        if m:
            hr, error = parse_target(m.group(1).replace(" ", "").replace("max.", "<=").replace("max", "<="))
            if hr is not None and error is None:
                return hr
    for text in texts:
        lower = text.lower()
        for words, zone in ZONE_WORDS:
            if any(w in lower for w in words):
                return parse_target(zone)[0]
    return defaults.target()


def main_part(row: PlanRow, defaults: Defaults) -> Step:
    hr = target_from_text(row.intensity, row.session, defaults=defaults)
    if row.duration_min is None:
        m = DISTANCE.search(row.session)
        if m:
            value = float(m.group(1).replace(",", "."))
            metres = int(round(value * 1000)) if m.group(2).lower() == "km" else int(round(value))
            return Step("active", distance_m=metres, hr=hr)
        return Step("active", duration_s=30 * 60, hr=hr)
    main_minutes = max(10, row.duration_min - defaults.warmup_min - defaults.cooldown_min)
    return Step("active", duration_s=main_minutes * 60, hr=hr)


def workout_from_row(row: PlanRow, day: date, defaults: Defaults) -> Workout:
    steps = [
        Step("warmup", duration_s=defaults.warmup_min * 60, hr=HeartRate("bpm", high=defaults.warmup_hr_max)),
        main_part(row, defaults),
        Step("cooldown", duration_s=defaults.cooldown_min * 60, hr=HeartRate("bpm", high=defaults.cooldown_hr_max)),
    ]
    session = re.sub(r"\s+", " ", row.session).strip()
    name = short_name(session) or "Run"
    description = "; ".join(part for part in (session if session != name else "", row.intensity.strip()) if part)
    return Workout(name=name, day=day, sport=guess_sport(name), steps=steps, description=description, status="draft")


def drafts_from_plan(conn: sqlite3.Connection, config: Config, *, week_start: date | None = None) -> DraftResult:
    """Write one draft per non-rest row of the newest weekly plan; existing files for a day are kept."""
    result = DraftResult()
    planned = last_weekly_plan(conn, "9999-12-31")
    rows = parse_plan_table(planned[1]) if planned else []
    if not rows:
        result.message = "no weekly plan stored yet (run wristpull coach weekly first)"
        return result
    result.week_start = week_start or (date.fromisoformat(planned[0]["period_end"]) + timedelta(days=1))
    defaults = config.workout.defaults()
    for row in sorted(rows, key=lambda r: r.day):
        day = result.week_start + timedelta(days=row.day)
        if row.is_rest:
            result.skipped.append(f"{day.isoformat()}: rest day")
            continue
        if existing_for_day(config, day):
            result.skipped.append(f"{day.isoformat()}: a workout file exists already")
            continue
        workout = workout_from_row(row, day, defaults)
        result.created.append(save(config, workout))
    return result
