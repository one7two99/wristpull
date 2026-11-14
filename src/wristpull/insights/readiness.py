"""A transparent readiness index: each component is a z-score against the own 28-day baseline, mapped to
0–100, weighted and averaged over the components that are present; the contributions explain the number."""

from __future__ import annotations

from typing import Any

# (key, column of the daily frame, direction: +1 higher is better / -1 lower is better / None special, weight)
COMPONENTS: tuple[tuple[str, str, int | None, float], ...] = (
    ("hrv", "hrv_last_night_avg_ms", 1, 0.25),
    ("resting_hr", "resting_hr", -1, 0.20),
    ("sleep_duration", "sleep_duration_s", 1, 0.15),
    ("sleep_score", "sleep_score", 1, 0.15),
    ("body_battery", "body_battery_max", 1, 0.10),
    ("stress", "stress_avg", -1, 0.10),
    ("acwr", "acwr", None, 0.05),
)
TITLES = {"hrv": "HRV last night", "resting_hr": "Resting HR", "sleep_duration": "Sleep", "sleep_score": "Sleep score", "body_battery": "Body Battery", "stress": "Stress (yesterday)", "acwr": "Acute:chronic load"}
Z_CLAMP = 2.5
POINTS_PER_Z = 20.0
SLEEP_FLOOR_H = 6.0
SLEEP_FLOOR_SCORE = 30.0
ACWR_FREE = 1.3
ACWR_ZERO = 1.8
MIN_COMPONENTS = 2
LABELS = ((70, "ready"), (45, "moderate"), (0, "recover"))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def component_score(key: str, direction: int | None, row: dict[str, Any], previous: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    """(value, z, score 0–100) of one component for a day; stress uses the day before, ACWR a penalty curve."""
    if key == "acwr":
        acwr = row.get("acwr")
        if acwr is None:
            return None, None, None
        score = 100.0 if acwr <= ACWR_FREE else max(0.0, 100.0 - 100.0 / (ACWR_ZERO - ACWR_FREE) * (acwr - ACWR_FREE))
        return float(acwr), None, round(score, 1)
    source = previous if key == "stress" else row
    if source is None:
        return None, None, None
    column = next(c for k, c, _d, _w in COMPONENTS if k == key)
    value, z = source.get(column), source.get(f"{column}_z")
    if value is None or z is None:
        return (float(value) if value is not None else None), None, None
    signed = clamp(float(z) * (direction or 1), -Z_CLAMP, Z_CLAMP)
    score = clamp(50.0 + POINTS_PER_Z * signed, 0.0, 100.0)
    if key == "sleep_duration" and float(value) < SLEEP_FLOOR_H * 3600:
        score = min(score, SLEEP_FLOOR_SCORE)
    return float(value), round(float(z), 2), round(score, 1)


def readiness_for(row: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Score, confidence and the contributions of one day (``None`` score with fewer than two components)."""
    components = []
    present_weight = 0.0
    total_weight = sum(w for _k, _c, _d, w in COMPONENTS)
    for key, _column, direction, weight in COMPONENTS:
        value, z, score = component_score(key, direction, row, previous)
        present = score is not None
        if present:
            present_weight += weight
        components.append({"key": key, "title": TITLES[key], "value": value, "z": z, "score": score, "weight": weight, "present": present, "contribution": None})
    present_components = [c for c in components if c["present"]]
    if len(present_components) < MIN_COMPONENTS or present_weight <= 0:
        return {"date": row.get("date"), "score": None, "label": None, "confidence": round(present_weight / total_weight, 2), "components": components}
    score = sum(c["weight"] * c["score"] for c in present_components) / present_weight
    for c in present_components:
        c["contribution"] = round(c["weight"] * (c["score"] - 50.0) / present_weight, 1)
    return {"date": row.get("date"), "score": round(score, 1), "label": label(score), "confidence": round(present_weight / total_weight, 2), "components": components}


def label(score: float | None) -> str | None:
    if score is None:
        return None
    return next(name for floor, name in LABELS if score >= floor)


def readiness_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Readiness for every row (rows sorted by date; the previous row feeds the stress component)."""
    out = []
    for i, row in enumerate(rows):
        out.append(readiness_for(row, rows[i - 1] if i > 0 else None))
    return out
