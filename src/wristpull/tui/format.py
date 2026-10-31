"""Language-aware number, date and duration formatting for the TUI (pure functions)."""

from __future__ import annotations

from datetime import date, datetime

from .i18n import current_language

DASH = "–"
SPARK = "▁▂▃▄▅▆▇█"
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _lang(lang: str | None) -> str:
    return lang or current_language()


def fmt_int(value: float | int | None, lang: str | None = None) -> str:
    if value is None:
        return DASH
    text = f"{int(round(value)):,}"
    return text.replace(",", " ") if _lang(lang) == "de" else text


def fmt_float(value: float | None, digits: int = 1, lang: str | None = None) -> str:
    if value is None:
        return DASH
    text = f"{value:,.{digits}f}"
    if _lang(lang) == "de":
        text = text.replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    return text


def fmt_duration(seconds: float | None) -> str:
    """Seconds -> h:mm (sleep, totals)."""
    if seconds is None:
        return DASH
    total = int(round(seconds / 60))
    return f"{total // 60}:{total % 60:02d}"


def fmt_clock(seconds: float | None) -> str:
    """Seconds -> m:ss or h:mm:ss (activity durations)."""
    if seconds is None:
        return DASH
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_bytes(size: int | None, lang: str | None = None) -> str:
    if size is None:
        return DASH
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            digits = 0 if unit == "B" else 1
            return f"{fmt_float(value, digits, lang)} {unit}"
        value /= 1024
    return DASH


def fmt_km(meters: float | None, lang: str | None = None) -> str:
    if meters is None:
        return DASH
    return f"{fmt_float(meters / 1000, 1, lang)} km"


def fmt_date(value: str | date | None, lang: str | None = None, *, year: bool = False) -> str:
    """ISO date (or date) -> '31.10.' / 'Oct 31'; with year '31.10.2026' / '2026-10-31'."""
    if value is None:
        return DASH
    day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if _lang(lang) == "de":
        return day.strftime("%d.%m.%Y") if year else day.strftime("%d.%m.")
    return day.isoformat() if year else f"{_MONTHS_EN[day.month - 1]} {day.day}"


def fmt_datetime(value: str | None, lang: str | None = None) -> str:
    """ISO UTC timestamp -> '31.10. 15:02' / 'Oct 31 15:02' (kept in UTC; the TUI shows it as stored)."""
    if not value:
        return DASH
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return f"{fmt_date(moment.date(), lang)} {moment:%H:%M}"


def sparkline(values: list[float | None], width: int | None = None) -> str:
    """Text sparkline; None renders as a space. Scales to the min/max of the present values."""
    data = values if width is None else values[-width:]
    present = [v for v in data if v is not None]
    if not present:
        return " " * len(data)
    lo, hi = min(present), max(present)
    span = hi - lo
    out = []
    for v in data:
        if v is None:
            out.append(" ")
        elif span == 0:
            out.append(SPARK[3])
        else:
            out.append(SPARK[min(7, int((v - lo) / span * 7.999))])
    return "".join(out)


def fmt_delta(current: float | None, previous: float | None, *, digits: int = 0, suffix: str = "", lang: str | None = None) -> str:
    """'▲ +3' / '▼ −2' / '–' comparing two period values."""
    if current is None or previous is None:
        return DASH
    diff = current - previous
    if abs(diff) < 10 ** (-digits) / 2:
        return "= 0" + suffix
    arrow = "▲" if diff > 0 else "▼"
    sign = "+" if diff > 0 else "−"
    return f"{arrow} {sign}{fmt_float(abs(diff), digits, lang) if digits else fmt_int(abs(diff), lang)}{suffix}"
