"""The fixed list of day tags (what the user did or felt) that the insights relate to the measurements."""

from __future__ import annotations

# key -> msgid (translated at render time); keep the keys stable, they are stored in day_tags
TAGS: tuple[tuple[str, str], ...] = (
    ("alcohol", "Alcohol"),
    ("alcohol_free", "No alcohol"),
    ("caffeine_late", "Caffeine late"),
    ("late_meal", "Late meal"),
    ("sick", "Sick"),
    ("travel", "Travel"),
    ("sauna", "Sauna"),
    ("nap", "Nap"),
    ("stress_high", "Stressful day"),
    ("rest_day", "Rest day"),
    ("race", "Race"),
)
KEYS = tuple(key for key, _label in TAGS)
LABELS = dict(TAGS)


def valid(tag: str) -> bool:
    return tag in LABELS
