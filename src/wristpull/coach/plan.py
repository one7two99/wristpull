"""The weekly plan as data: parse the Mon–Sun table from a weekly answer, compare it with what happened, export it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DAY_NAMES = {
    0: ("mon", "monday", "mo", "montag"),
    1: ("tue", "tuesday", "di", "dienstag"),
    2: ("wed", "wednesday", "mi", "mittwoch"),
    3: ("thu", "thursday", "do", "donnerstag"),
    4: ("fri", "friday", "fr", "freitag"),
    5: ("sat", "saturday", "sa", "samstag"),
    6: ("sun", "sunday", "so", "sonntag"),
}
REST_WORDS = ("rest", "ruhe", "pause", "off", "frei", "recovery day", "ruhetag")
DURATION = re.compile(r"(\d+(?:[.,]\d+)?)\s*(h|hr|hrs|hour|hours|std|min|mins|minutes|minuten|m)\b", re.I)
CLOCK_DURATION = re.compile(r"\b(\d{1,2}):(\d{2})\s*(?:h|hr|std)?\b", re.I)


@dataclass
class PlanRow:
    day: int  # 0 = Monday
    session: str
    duration_min: int | None
    intensity: str

    @property
    def is_rest(self) -> bool:
        text = self.session.lower()
        return any(w in text for w in REST_WORDS) and not any(k in text for k in ("run", "lauf", "ride", "rad", "swim", "strength", "kraft"))


def _day_index(cell: str) -> int | None:
    token = re.sub(r"[^a-zäöü]", "", cell.strip().lower())
    for index, names in DAY_NAMES.items():
        if token in names or any(token.startswith(n) for n in names if len(n) >= 3):
            return index
    return None


def parse_duration(text: str) -> int | None:
    """Minutes from ``45 min``, ``1.5 h``, ``1 h 15 min`` or the clock form ``1:30 h``."""
    clock = CLOCK_DURATION.search(text)
    if clock:
        return int(clock.group(1)) * 60 + int(clock.group(2))
    total = 0.0
    found = False
    for m in DURATION.finditer(text):
        value = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        total += value * 60 if unit.startswith(("h", "std")) else value
        found = True
    return int(round(total)) if found else None


def parse_plan_table(markdown: str) -> list[PlanRow]:
    """Rows of the first Markdown table whose first column names weekdays (any language the coach uses)."""
    rows: list[PlanRow] = []
    for line in markdown.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= set("-: "):
            continue
        day = _day_index(cells[0])
        if day is None:
            continue
        session = cells[1]
        duration_cell = cells[2] if len(cells) > 2 else ""
        intensity = cells[3] if len(cells) > 3 else ""
        duration = parse_duration(duration_cell) or parse_duration(session)
        rows.append(PlanRow(day, session, duration, intensity))
    return rows


def adherence(plan: list[PlanRow], week_start: date, activities: list[dict[str, Any]], tz: ZoneInfo) -> list[str]:
    """One line per planned day: what was planned and what the activities table shows for that date."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for a in activities:
        by_day.setdefault(str(a.get("local_date") or a.get("date") or "")[:10], []).append(a)
    lines: list[str] = []
    for row in sorted(plan, key=lambda r: r.day):
        day = week_start + timedelta(days=row.day)
        done = by_day.get(day.isoformat(), [])
        planned = f"{row.session}" + (f" {row.duration_min} min" if row.duration_min else "") + (f", {row.intensity}" if row.intensity else "")
        if done:
            parts = []
            for a in done:
                minutes = a.get("duration_min")
                if minutes is None and (a.get("timer_s") or a.get("elapsed_s")):
                    minutes = round((a.get("timer_s") or a.get("elapsed_s")) / 60)
                parts.append(f"{a.get('sport') or 'activity'} {int(minutes) if minutes else '?'} min" + (f", avg HR {int(a['avg_hr'])}" if a.get("avg_hr") else ""))
            actual = "; ".join(parts)
        else:
            actual = "rest" if row.is_rest else "nothing recorded"
        mark = "✓" if (done and not row.is_rest) or (not done and row.is_rest) else "✗"
        lines.append(f"- {day.strftime('%a %Y-%m-%d')}: planned {planned} → {actual} {mark}")
    return lines


def plan_to_ics(plan: list[PlanRow], week_start: date, tz: ZoneInfo, *, hour: int = 7, title_prefix: str = "wristpull") -> str:
    """An iCalendar file with one event per planned day (rest days as all-day notes)."""
    stamp = datetime.now(tz).strftime("%Y%m%dT%H%M%S")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//wristpull//coach plan//EN", "CALSCALE:GREGORIAN"]
    for row in sorted(plan, key=lambda r: r.day):
        day = week_start + timedelta(days=row.day)
        uid = f"wristpull-{day.isoformat()}@local"
        summary = f"{title_prefix}: {row.session}" + (f" ({row.duration_min} min)" if row.duration_min else "")
        description = (row.intensity or "").replace("\n", " ")
        out += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"SUMMARY:{_ics_escape(summary)}"]
        if row.is_rest or not row.duration_min:
            out += [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"]
        else:
            start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            end = start + timedelta(minutes=row.duration_min)
            out += [f"DTSTART;TZID={tz.key}:{start.strftime('%Y%m%dT%H%M%S')}", f"DTEND;TZID={tz.key}:{end.strftime('%Y%m%dT%H%M%S')}"]
        if description:
            out.append(f"DESCRIPTION:{_ics_escape(description)}")
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
