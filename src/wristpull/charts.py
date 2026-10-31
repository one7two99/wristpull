"""Tiny dependency-free SVG charts for reports (bars and lines)."""

from __future__ import annotations

from collections.abc import Sequence

W, H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 28, 44
FONT = "font-family='system-ui, sans-serif' font-size='12'"


def _scale(values: Sequence[float | None]) -> tuple[float, float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return 0.0, 1.0
    lo, hi = min(nums), max(nums)
    if lo == hi:
        hi = lo + 1
    return (0.0 if lo >= 0 else lo), hi


def _axis(title: str, lo: float, hi: float, unit: str) -> str:
    plot_h = H - PAD_T - PAD_B
    parts = [f"<text x='{PAD_L}' y='18' {FONT} font-weight='600'>{title}</text>"]
    for i in range(5):
        value = lo + (hi - lo) * i / 4
        y = PAD_T + plot_h - plot_h * i / 4
        parts.append(f"<line x1='{PAD_L}' y1='{y:.1f}' x2='{W - PAD_R}' y2='{y:.1f}' stroke='#ddd' stroke-width='1'/>")
        label = f"{value:.0f}" if hi - lo >= 10 else f"{value:.1f}"
        parts.append(f"<text x='{PAD_L - 6}' y='{y + 4:.1f}' text-anchor='end' {FONT} fill='#555'>{label}{unit}</text>")
    return "".join(parts)


def _x_labels(labels: Sequence[str], step_x: float) -> str:
    every = max(1, len(labels) // 10)
    out = []
    for i, label in enumerate(labels):
        if i % every:
            continue
        x = PAD_L + step_x * (i + 0.5)
        out.append(f"<text x='{x:.1f}' y='{H - PAD_B + 16}' text-anchor='middle' {FONT} fill='#555'>{label}</text>")
    return "".join(out)


def bar_chart(title: str, labels: Sequence[str], values: Sequence[float | None], *, unit: str = "", color: str = "#3b7ddd") -> str:
    lo, hi = _scale(values)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    step_x = plot_w / max(1, len(values))
    bars = []
    for i, v in enumerate(values):
        if v is None:
            continue
        h = plot_h * (v - lo) / (hi - lo)
        x = PAD_L + step_x * i + step_x * 0.15
        bars.append(f"<rect x='{x:.1f}' y='{PAD_T + plot_h - h:.1f}' width='{step_x * 0.7:.1f}' height='{h:.1f}' fill='{color}' rx='2'><title>{labels[i]}: {v:g}{unit}</title></rect>")
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {W} {H}' width='{W}' height='{H}' role='img' aria-label='{title}'>"
        f"<rect width='{W}' height='{H}' fill='white'/>" + _axis(title, lo, hi, unit) + "".join(bars) + _x_labels(labels, step_x) + "</svg>"
    )


def line_chart(title: str, labels: Sequence[str], series: dict[str, Sequence[float | None]], *, unit: str = "", colors: Sequence[str] = ("#d9534f", "#3b7ddd", "#5cb85c")) -> str:
    all_values = [v for vals in series.values() for v in vals]
    lo, hi = _scale(all_values)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    step_x = plot_w / max(1, len(labels))
    parts = []
    legend_x = W - PAD_R
    for idx, (name, vals) in enumerate(series.items()):
        color = colors[idx % len(colors)]
        points = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            x = PAD_L + step_x * (i + 0.5)
            y = PAD_T + plot_h - plot_h * (v - lo) / (hi - lo)
            points.append((x, y, labels[i], v))
        if points:
            path = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y, _, _) in enumerate(points))
            parts.append(f"<path d='{path}' fill='none' stroke='{color}' stroke-width='2'/>")
            parts.extend(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3' fill='{color}'><title>{label}: {v:g}{unit}</title></circle>" for x, y, label, v in points)
        legend_x -= 14 * len(name) * 0.6 + 24
        parts.append(f"<rect x='{legend_x:.1f}' y='8' width='12' height='12' fill='{color}'/><text x='{legend_x + 16:.1f}' y='18' {FONT}>{name}</text>")
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {W} {H}' width='{W}' height='{H}' role='img' aria-label='{title}'>"
        f"<rect width='{W}' height='{H}' fill='white'/>" + _axis(title, lo, hi, unit) + "".join(parts) + _x_labels(labels, step_x) + "</svg>"
    )
