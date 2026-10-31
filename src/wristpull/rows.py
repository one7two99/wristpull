"""Parsed-row containers shared by the FIT and Gadgetbridge parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SAMPLE_TABLES = (
    "heart_rate",
    "resting_hr",
    "steps",
    "intensity_minutes",
    "stress",
    "body_battery",
    "hrv",
    "hrv_values",
    "spo2",
    "respiration",
    "temperature",
    "metrics",
)


@dataclass
class ParsedActivity:
    row: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    laps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedSleep:
    row: dict[str, Any]
    stages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedRows:
    activities: list[ParsedActivity] = field(default_factory=list)
    sleep: list[ParsedSleep] = field(default_factory=list)
    heart_rate: list[dict[str, Any]] = field(default_factory=list)
    resting_hr: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    intensity_minutes: list[dict[str, Any]] = field(default_factory=list)
    stress: list[dict[str, Any]] = field(default_factory=list)
    body_battery: list[dict[str, Any]] = field(default_factory=list)
    hrv: list[dict[str, Any]] = field(default_factory=list)
    hrv_values: list[dict[str, Any]] = field(default_factory=list)
    spo2: list[dict[str, Any]] = field(default_factory=list)
    respiration: list[dict[str, Any]] = field(default_factory=list)
    temperature: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    # tables whose rows may legitimately change between imports (derived cumulative counters)
    replace_tables: set[str] = field(default_factory=set)

    def counts(self) -> dict[str, int]:
        out = {name: len(getattr(self, name)) for name in SAMPLE_TABLES}
        out["activities"] = len(self.activities)
        out["sleep"] = len(self.sleep)
        out["activity_records"] = sum(len(a.records) for a in self.activities)
        out["activity_laps"] = sum(len(a.laps) for a in self.activities)
        out["sleep_stages"] = sum(len(s.stages) for s in self.sleep)
        return {k: v for k, v in out.items() if v}
