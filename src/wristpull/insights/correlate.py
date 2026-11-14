"""Lagged correlations between the day's metrics, a binned response curve and the "sweet spot".

Both variables enter as their z-scores against the own trailing baseline (within-person deviations), so a
slow drift over months does not create a spurious correlation. Effects are reported with their strength and
n; below ``MIN_PAIRS`` paired days the result says "insufficient" instead of guessing.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

# (x on day d, y on day d + lag). The sleep columns of a date describe the night that ended on that date,
# so "sleep -> same-day resting HR" is lag 0 and "sleep -> the next night's HRV" is lag 1.
PAIRS: tuple[tuple[str, str, int], ...] = (
    ("sleep_duration_s", "resting_hr", 0),
    ("sleep_duration_s", "body_battery_max", 0),
    ("sleep_duration_s", "stress_avg", 0),
    ("sleep_duration_s", "hrv_last_night_avg_ms", 1),
    ("training_load", "sleep_score", 1),
    ("training_load", "hrv_last_night_avg_ms", 1),
    ("stress_avg", "sleep_score", 1),
    ("bed_offset", "sleep_score", 0),
)
BETTER = {"hrv_last_night_avg_ms": 1, "resting_hr": -1, "stress_avg": -1, "sleep_score": 1, "body_battery_max": 1}
TITLES = {"sleep_duration_s": "Sleep", "resting_hr": "Resting HR", "body_battery_max": "Body Battery", "stress_avg": "Stress", "hrv_last_night_avg_ms": "HRV last night",
          "training_load": "Training load", "sleep_score": "Sleep score", "bed_offset": "Bedtime"}
UNITS = {"sleep_duration_s": "h", "resting_hr": "bpm", "body_battery_max": "", "stress_avg": "", "hrv_last_night_avg_ms": "ms", "training_load": "", "sleep_score": "", "bed_offset": "clock"}
SCALE = {"sleep_duration_s": 3600.0}  # raw values shown divided by this
MIN_PAIRS = 20
MIN_BIN = 5
SWEET_MARGIN_Z = 0.25
LAGS = (0, 1, 2)
FIXED_EDGES = {"sleep_duration_s": [6.0, 6.5, 7.0, 7.5, 8.0]}  # hours; bedtime gets one-hour bins, the rest quartiles


def shown(x: str, value: float) -> float:
    return value / SCALE.get(x, 1.0)


def paired(rows: list[dict[str, Any]], x: str, y: str, lag: int) -> list[dict[str, float]]:
    """Days where x (day d) and y (day d + lag) both have a z-score and a value."""
    out = []
    for i, row in enumerate(rows):
        j = i + lag
        if j >= len(rows):
            break
        other = rows[j]
        xz, yz = row.get(f"{x}_z"), other.get(f"{y}_z")
        xv, yv = row.get(x), other.get(y)
        if None in (xz, yz, xv, yv):
            continue
        out.append({"x": shown(x, float(xv)), "y": float(yv), "x_z": float(xz), "y_z": float(yz), "date": row.get("date")})
    return out


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(_ranks(xs), _ranks(ys))


def strength(r: float | None, n: int) -> dict[str, Any]:
    """Plain-language strength of a correlation plus a rough t-value (|t| > 2 counts as significant)."""
    if r is None or n < MIN_PAIRS:
        return {"label": "insufficient", "direction": None, "significant": False, "t": None}
    size = abs(r)
    label = "none" if size < 0.1 else ("weak" if size < 0.3 else ("moderate" if size < 0.5 else "strong"))
    t = r * math.sqrt((n - 2) / max(1e-9, 1 - r * r)) if size < 1 else float("inf")
    return {"label": label, "direction": "+" if r > 0 else ("-" if r < 0 else None), "significant": abs(t) > 2, "t": round(t, 2) if math.isfinite(t) else None}


def edges_for(x: str, values: list[float]) -> list[float]:
    """Bin edges for the response curve: fixed hours for sleep, 30-minute steps for bedtime, else quartiles."""
    if x in FIXED_EDGES:
        return list(FIXED_EDGES[x])
    if not values:
        return []
    if x == "bed_offset":  # one-hour bins on the clock, from the earliest full hour to the latest
        lo, hi = min(values), max(values)
        start = math.floor(lo / 60) * 60 + 60
        return [float(e) for e in range(int(start), int(math.ceil(hi / 60) * 60), 60)]
    if x == "training_load":  # rest days are their own bin, the training days split into tertiles
        positive = sorted(v for v in values if v > 0)
        inner = statistics.quantiles(positive, n=3) if len(positive) >= 3 else []
        return [1.0] + [round(q, 1) for q in inner if q > 1.0]
    quantiles = statistics.quantiles(values, n=4) if len(values) >= 4 else []
    edges = []
    for q in quantiles:
        if not edges or q > edges[-1]:
            edges.append(round(q, 2))
    return edges


def bin_label(x: str, lo: float | None, hi: float | None) -> str:
    def fmt(v: float) -> str:
        if x == "bed_offset":
            total = (int(round(v)) + 18 * 60) % (24 * 60)
            return f"{total // 60:02d}:{total % 60:02d}"
        return f"{v:g}"

    if lo is None:
        return "0 (rest)" if x == "training_load" else f"< {fmt(hi)}"
    if hi is None:
        return f"≥ {fmt(lo)}"
    return f"{fmt(lo)}–{fmt(hi)}"


def response_curve(pairs: list[dict[str, float]], x: str, edges: list[float]) -> list[dict[str, Any]]:
    """Mean and median of y (raw and as z) per bin of x; bins with no pair are left out."""
    bounds: list[tuple[float | None, float | None]] = []
    if edges:
        bounds.append((None, edges[0]))
        bounds += [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
        bounds.append((edges[-1], None))
    else:
        bounds.append((None, None))
    curve = []
    for lo, hi in bounds:
        chosen = [p for p in pairs if (lo is None or p["x"] >= lo) and (hi is None or p["x"] < hi)]
        if not chosen:
            continue
        ys = [p["y"] for p in chosen]
        curve.append({"label": bin_label(x, lo, hi), "lo": lo, "hi": hi, "n": len(chosen), "mean_y": round(statistics.mean(ys), 1),
                      "median_y": round(statistics.median(ys), 1), "mean_y_z": round(statistics.mean(p["y_z"] for p in chosen), 2)})
    return curve


def sweet_spot(curve: list[dict[str, Any]], better: int, *, min_n: int = MIN_BIN, margin_z: float = SWEET_MARGIN_Z) -> dict[str, Any] | None:
    """The bin with the best mean y (in the "better" direction) among bins with enough days, if it beats both
    neighbours clearly; otherwise None ("no clear sweet spot")."""
    eligible = [i for i, b in enumerate(curve) if b["n"] >= min_n]
    if len(eligible) < 2:
        return None
    best = max(eligible, key=lambda i: better * curve[i]["mean_y_z"])
    neighbours = [i for i in eligible if abs(i - best) == 1 or (i != best and not any(abs(j - best) == 1 for j in eligible))]
    if not neighbours:
        return None
    margin = min(better * (curve[best]["mean_y_z"] - curve[i]["mean_y_z"]) for i in neighbours)
    if margin < margin_z:
        return None
    return {"bin": curve[best]["label"], "mean_y": curve[best]["mean_y"], "n": curve[best]["n"], "margin_z": round(margin, 2)}


def correlation(rows: list[dict[str, Any]], x: str, y: str, lag: int, *, min_n: int = MIN_PAIRS) -> dict[str, Any]:
    """One pair: Spearman r over the within-person z-scores, strength, the r at lags 0–2, the response curve
    and the sweet spot (both on raw x bins, y in raw units and z)."""
    pairs = paired(rows, x, y, lag)
    n = len(pairs)
    r = spearman([p["x_z"] for p in pairs], [p["y_z"] for p in pairs]) if n >= min_n else None
    by_lag = {}
    for other in LAGS:
        ps = paired(rows, x, y, other)
        rl = spearman([p["x_z"] for p in ps], [p["y_z"] for p in ps]) if len(ps) >= min_n else None
        by_lag[str(other)] = round(rl, 2) if rl is not None else None
    curve = response_curve(pairs, x, edges_for(x, [p["x"] for p in pairs])) if n >= min_n else []
    better = BETTER.get(y, 1)
    return {
        "x": x, "y": y, "lag": lag, "x_title": TITLES.get(x, x), "y_title": TITLES.get(y, y), "x_unit": UNITS.get(x, ""), "y_unit": UNITS.get(y, ""),
        "n": n, "r": round(r, 2) if r is not None else None, "method": "spearman", **strength(r, n), "by_lag": by_lag,
        "better": better, "bins": curve, "sweet_spot": sweet_spot(curve, better) if curve else None,
        "points": [[round(p["x"], 2), round(p["y"], 1)] for p in pairs],
    }


def tag_effects(rows: list[dict[str, Any]], tags_by_date: dict[str, set[str]], metrics: tuple[str, ...] = ("hrv_last_night_avg_ms", "resting_hr", "sleep_score", "body_battery_max"), *, lag: int = 1, min_n: int = MIN_BIN) -> list[dict[str, Any]]:
    """Per tag: mean z (and raw) of each metric on the day after tagged days against untagged days."""
    out = []
    all_tags = sorted({t for tags in tags_by_date.values() for t in tags})
    by_date = {row["date"]: i for i, row in enumerate(rows)}
    for tag in all_tags:
        entry: dict[str, Any] = {"tag": tag, "n": 0, "effects": []}
        tagged_idx = [by_date[d] + lag for d, tags in tags_by_date.items() if tag in tags and d in by_date and by_date[d] + lag < len(rows)]
        tagged_set = set(tagged_idx)
        entry["n"] = len(tagged_idx)
        if entry["n"] < min_n:
            out.append(entry)
            continue
        for m in metrics:
            with_tag = [rows[i] for i in tagged_idx if rows[i].get(f"{m}_z") is not None]
            without = [row for i, row in enumerate(rows) if i not in tagged_set and row.get(f"{m}_z") is not None]
            if len(with_tag) < min_n or len(without) < min_n:
                continue
            dz = statistics.mean(r[f"{m}_z"] for r in with_tag) - statistics.mean(r[f"{m}_z"] for r in without)
            draw = statistics.mean(r[m] for r in with_tag) - statistics.mean(r[m] for r in without)
            entry["effects"].append({"metric": m, "title": TITLES.get(m, m), "unit": UNITS.get(m, ""), "n": len(with_tag), "delta_z": round(dz, 2), "delta": round(draw, 1),
                                     "better": BETTER.get(m, 1)})
        out.append(entry)
    return out
