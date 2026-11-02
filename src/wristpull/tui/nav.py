"""The ten screens in display order: digit key, screen name, label (translated at render time)."""

from __future__ import annotations

from .i18n import _

NAV: tuple[tuple[str, str, str], ...] = (
    ("1", "home", "Overview"),
    ("2", "sync", "Devices & Sync"),
    ("3", "days", "Days"),
    ("4", "activities", "Activities"),
    ("5", "sleep", "Sleep"),
    ("6", "files", "Files"),
    ("7", "export", "Export & Report"),
    ("8", "config", "Settings"),
    ("9", "coach", "Coach"),
    ("0", "workouts", "Workouts"),
)


def nav_label(digit: str, label: str) -> str:
    return f"{digit} {_(label)}"


def tab_id(name: str) -> str:
    return f"nav-{name}"
