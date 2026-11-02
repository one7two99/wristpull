"""Workout data model and the Markdown format (one file per workout).

    ---
    date: 2026-11-05
    sport: running
    name: Easy run
    status: draft
    ---
    # Easy run
    - warmup 10 min hr <= 130
    - run 40 min hr 135-150
    - cooldown 5 min hr <= 125

Each step is ``- <intensity> <duration> [hr <target>] [# note]``. Durations are
minutes (``40 min``, ``1:15 h``), distances (``5000 m``, ``5 km``) or ``open``. Heart-rate
targets are bpm ranges (``135-150``), bounds (``<= 130``, ``>= 120``), Garmin zones (``Z2``)
or percent of the maximum heart rate (``70-80 %``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

STATUSES = ("draft", "ready", "pushed", "verified")
INTENSITIES = ("warmup", "active", "recovery", "cooldown", "rest")
INTENSITY_WORDS: dict[str, str] = {
    "warmup": "warmup", "warm-up": "warmup", "aufwärmen": "warmup", "aufwaermen": "warmup", "einlaufen": "warmup",
    "run": "active", "active": "active", "main": "active", "work": "active", "interval": "active", "laufen": "active", "lauf": "active", "hauptteil": "active",
    "recovery": "recovery", "recover": "recovery", "erholung": "recovery", "trab": "recovery",
    "cooldown": "cooldown", "cool-down": "cooldown", "auslaufen": "cooldown", "abwärmen": "cooldown",
    "rest": "rest", "pause": "rest", "ruhe": "rest",
}
SPORTS = ("running",)
MAX_NAME_LEN = 30
HR_BPM_MIN, HR_BPM_MAX = 30, 230

STEP_RE = re.compile(
    r"^\s*[-*]\s+(?P<intensity>[^\s]+)\s+(?P<duration>open|offen|\d+(?:[.,]\d+)?\s*(?:min|m|km|h|s)|\d+:\d{2}\s*h?)"
    r"(?:\s+(?:hr|hf|puls)\s+(?P<target>[^#]+?))?\s*(?:#\s*(?P<note>.*))?\s*$",
    re.I,
)
TARGET_RANGE = re.compile(r"^(\d+)\s*[-–]\s*(\d+)\s*(%)?$")
TARGET_BOUND = re.compile(r"^(<=|<|>=|>|max|min)\s*(\d+)\s*(%)?$", re.I)
TARGET_ZONE = re.compile(r"^z(?:one)?\s*([1-5])$", re.I)
FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class HeartRate:
    """A heart-rate target: a zone, a bpm range or a percent-of-max range (one of the three)."""

    kind: str  # zone | bpm | percent
    low: int | None = None
    high: int | None = None
    zone: int | None = None

    def text(self) -> str:
        if self.kind == "zone":
            return f"Z{self.zone}"
        unit = " %" if self.kind == "percent" else ""
        low = self.low if self.low is not None else (HR_BPM_MIN if self.kind == "bpm" else 1)
        high = self.high if self.high is not None else (HR_BPM_MAX if self.kind == "bpm" else 100)
        if self.low is None:
            return f"<= {high}{unit}"
        if self.high is None:
            return f">= {low}{unit}"
        return f"{low}-{high}{unit}"


@dataclass
class Step:
    intensity: str  # warmup | active | recovery | cooldown | rest
    duration_s: int | None = None  # time steps
    distance_m: int | None = None  # distance steps (both None: open, ends with the lap key)
    hr: HeartRate | None = None
    note: str = ""

    @property
    def kind(self) -> str:
        if self.duration_s is not None:
            return "time"
        if self.distance_m is not None:
            return "distance"
        return "open"

    def duration_text(self) -> str:
        if self.duration_s is not None:
            minutes, seconds = divmod(self.duration_s, 60)
            return f"{minutes} min" if not seconds else f"{minutes}:{seconds:02d} min"
        if self.distance_m is not None:
            return f"{self.distance_m / 1000:g} km" if self.distance_m % 100 == 0 and self.distance_m >= 1000 else f"{self.distance_m} m"
        return "open"


@dataclass
class Workout:
    name: str
    day: date | None = None
    sport: str = "running"
    status: str = "draft"
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    path: str | None = None  # where it was loaded from
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def total_seconds(self) -> int:
        return sum(s.duration_s or 0 for s in self.steps)

    @property
    def total_metres(self) -> int:
        return sum(s.distance_m or 0 for s in self.steps)


# ---- parsing ---------------------------------------------------------------------------


def parse_duration(text: str) -> tuple[int | None, int | None, str | None]:
    """``(seconds, metres, error)`` for a duration token; open steps return (None, None, None)."""
    token = text.strip().lower().replace(",", ".")
    if token in ("open", "offen"):
        return None, None, None
    m = re.match(r"^(\d+):(\d{2})\s*h?$", token)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60, None, None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(min|m|km|h|s)$", token)
    if not m:
        return None, None, f"unreadable duration {text.strip()!r}"
    value, unit = float(m.group(1)), m.group(2)
    if unit == "min":
        return int(round(value * 60)), None, None
    if unit == "h":
        return int(round(value * 3600)), None, None
    if unit == "s":
        return int(round(value)), None, None
    if unit == "km":
        return None, int(round(value * 1000)), None
    return None, int(round(value)), None


def parse_target(text: str) -> tuple[HeartRate | None, str | None]:
    """Heart-rate target from ``135-150``, ``<= 130``, ``>= 120``, ``Z2`` or ``70-80 %``."""
    token = text.strip().replace("bpm", "").strip()
    m = TARGET_ZONE.match(token)
    if m:
        return HeartRate("zone", zone=int(m.group(1))), None
    m = TARGET_RANGE.match(token)
    if m:
        low, high, pct = int(m.group(1)), int(m.group(2)), bool(m.group(3))
        hr = HeartRate("percent" if pct else "bpm", low=low, high=high)
        return hr, _check_hr(hr)
    m = TARGET_BOUND.match(token)
    if m:
        op, value, pct = m.group(1).lower(), int(m.group(2)), bool(m.group(3))
        kind = "percent" if pct else "bpm"
        hr = HeartRate(kind, high=value) if op in ("<=", "<", "max") else HeartRate(kind, low=value)
        return hr, _check_hr(hr)
    return None, f"unreadable heart-rate target {text.strip()!r}"


def _check_hr(hr: HeartRate) -> str | None:
    lo, hi = (1, 100) if hr.kind == "percent" else (HR_BPM_MIN, HR_BPM_MAX)
    unit = "%" if hr.kind == "percent" else "bpm"
    for value in (hr.low, hr.high):
        if value is not None and not lo <= value <= hi:
            return f"heart rate {value} {unit} outside {lo}-{hi}"
    if hr.low is not None and hr.high is not None and hr.low >= hr.high:
        return f"heart-rate range {hr.low}-{hr.high} must go from low to high"
    return None


def parse_step(line: str) -> tuple[Step | None, str | None]:
    m = STEP_RE.match(line)
    if not m:
        return None, f"unreadable step {line.strip()!r} (expected '- <intensity> <duration> [hr <target>]')"
    word = m.group("intensity").lower()
    intensity = INTENSITY_WORDS.get(word)
    if intensity is None:
        return None, f"unknown intensity {word!r} (use warmup, run, recovery, cooldown or rest)"
    seconds, metres, error = parse_duration(m.group("duration"))
    if error:
        return None, error
    if seconds == 0 or metres == 0:
        return None, f"step {line.strip()!r} has no length"
    hr: HeartRate | None = None
    if m.group("target"):
        hr, error = parse_target(m.group("target"))
        if error:
            return None, error
    return Step(intensity, seconds, metres, hr, (m.group("note") or "").strip()), None


def parse_markdown(text: str, *, path: str | None = None) -> Workout:
    """Parse one workout file; problems land in ``workout.errors`` instead of raising."""
    meta: dict[str, str] = {}
    body = text
    fm = FRONT_MATTER.match(text)
    if fm:
        body = text[fm.end():]
        for raw in fm.group(1).splitlines():
            if ":" in raw and not raw.lstrip().startswith("#"):
                key, _sep, value = raw.partition(":")
                meta[key.strip().lower()] = value.split("#", 1)[0].strip().strip('"').strip("'")
    errors: list[str] = []
    day: date | None = None
    if meta.get("date"):
        try:
            day = date.fromisoformat(meta["date"])
        except ValueError:
            errors.append(f"invalid date {meta['date']!r} (YYYY-MM-DD)")
    else:
        errors.append("missing 'date:' in the front matter")
    name = meta.get("name") or ""
    description_lines: list[str] = []
    steps: list[Step] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not name:
                name = stripped.lstrip("#").strip()
            continue
        if stripped.startswith(("-", "*")):
            step, error = parse_step(line)
            if step is not None:
                steps.append(step)
            else:
                errors.append(error or "unreadable step")
            continue
        description_lines.append(stripped)
    if not name:
        errors.append("missing name (front matter 'name:' or a '# heading')")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"name longer than {MAX_NAME_LEN} characters (the watch truncates it)")
    sport = (meta.get("sport") or "running").lower()
    if sport not in SPORTS:
        errors.append(f"unsupported sport {sport!r} (only: {', '.join(SPORTS)})")
    status = (meta.get("status") or "draft").lower()
    if status not in STATUSES:
        errors.append(f"unknown status {status!r} (one of {', '.join(STATUSES)})")
    if not steps:
        errors.append("no steps found")
    return Workout(name=name, day=day, sport=sport, status=status, steps=steps, description=" ".join(description_lines), path=path, errors=errors)


# ---- rendering -------------------------------------------------------------------------

STEP_WORD = {"warmup": "warmup", "active": "run", "recovery": "recovery", "cooldown": "cooldown", "rest": "rest"}


def render_step(step: Step) -> str:
    text = f"- {STEP_WORD[step.intensity]} {step.duration_text()}"
    if step.hr is not None:
        text += f" hr {step.hr.text()}"
    if step.note:
        text += f"  # {step.note}"
    return text


def render_markdown(workout: Workout) -> str:
    lines = ["---", f"date: {workout.day.isoformat() if workout.day else ''}", f"sport: {workout.sport}", f"name: {workout.name}", f"status: {workout.status}", "---", f"# {workout.name}"]
    if workout.description:
        lines += ["", workout.description, ""]
    lines += [render_step(s) for s in workout.steps]
    return "\n".join(lines) + "\n"


def with_status(workout: Workout, status: str) -> Workout:
    return replace(workout, status=status)


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")).strip("-")
    return text or "workout"


def file_name(workout: Workout) -> str:
    return f"{workout.day.isoformat() if workout.day else 'undated'}-{slug(workout.name)}.md"


# ---- templates -------------------------------------------------------------------------


@dataclass(frozen=True)
class Defaults:
    """Shape of a generated workout (from ``[workout]`` in the config)."""

    warmup_min: int = 10
    cooldown_min: int = 5
    warmup_hr_max: int = 130
    cooldown_hr_max: int = 125
    default_target: str = "Z2"

    def target(self) -> HeartRate | None:
        hr, _error = parse_target(self.default_target) if self.default_target else (None, None)
        return hr


def template(kind: str, day: date, defaults: Defaults, *, name: str | None = None, main_minutes: int = 40, main_metres: int = 5000, target: HeartRate | None = None) -> Workout:
    """A three-step workout: warm-up by time, main part by time (``kind='time'``) or distance, cool-down by time."""
    hr = target or defaults.target()
    if kind == "distance":
        main = Step("active", distance_m=main_metres, hr=hr)
        title = name or f"Run {main_metres / 1000:g} km"
    else:
        main = Step("active", duration_s=main_minutes * 60, hr=hr)
        title = name or f"Easy run {main_minutes} min"
    steps = [
        Step("warmup", duration_s=defaults.warmup_min * 60, hr=HeartRate("bpm", high=defaults.warmup_hr_max)),
        main,
        Step("cooldown", duration_s=defaults.cooldown_min * 60, hr=HeartRate("bpm", high=defaults.cooldown_hr_max)),
    ]
    return Workout(name=title[:MAX_NAME_LEN], day=day, steps=steps)


def describe_steps(workout: Workout) -> list[str]:
    """One line per step the way the watch will show it (``1 warmup 10:00  HR <= 130``)."""
    lines: list[str] = []
    for index, step in enumerate(workout.steps, 1):
        if step.duration_s is not None:
            length = f"{step.duration_s // 60}:{step.duration_s % 60:02d}"
        elif step.distance_m is not None:
            length = f"{step.distance_m} m"
        else:
            length = "lap key"
        text = f"{index} {step.intensity} {length}"
        if step.hr is not None:
            text += f"  HR {step.hr.text()}"
        if step.note:
            text += f"  ({step.note})"
        lines.append(text)
    return lines
