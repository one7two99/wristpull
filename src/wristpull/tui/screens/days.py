"""Screen 3: one row per day from daily_summary (merged Garmin + strap) with a detail pane."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable

from ..format import fmt_date, fmt_duration, fmt_float, fmt_int
from ..i18n import _
from ..queries import day_rows
from ..widgets import Panel
from .base import PeriodMixin, WristpullScreen

DETAIL_FIELDS: tuple[tuple[str, str, str], ...] = (
    # (label msgid, garmin column, kind) kind: int | float | duration | km
    ("Steps", "steps", "int"),
    ("Distance", "distance_m", "km"),
    ("Active calories", "active_calories", "int"),
    ("Moderate minutes", "moderate_min", "int"),
    ("Vigorous minutes", "vigorous_min", "int"),
    ("HR min / avg / max", "hr", "hr"),
    ("Resting HR", "resting_hr", "int"),
    ("Stress avg / max", "stress", "stress"),
    ("Body battery min / max", "body_battery", "bb"),
    ("SpO2 avg / min", "spo2", "spo2"),
    ("Respiration avg", "respiration_avg", "float"),
    ("Temperature avg", "temperature_avg", "float"),
    ("Sleep", "sleep_duration_s", "duration"),
    ("Sleep score", "sleep_score", "int"),
    ("HRV last night", "hrv_last_night_avg_ms", "float"),
    ("Activities", "activities_count", "int"),
    ("Training load", "training_load", "float"),
)


def _value(row: dict[str, Any], prefix: str, column: str, kind: str) -> str:
    g = lambda name: row.get(f"{prefix}_{name}")  # noqa: E731
    if kind == "int":
        return fmt_int(g(column))
    if kind == "float":
        return fmt_float(g(column), 1)
    if kind == "km":
        v = g(column)
        return "–" if v is None else f"{fmt_float(v / 1000, 1)} km"
    if kind == "duration":
        v = g(column)
        return "–" if v is None else fmt_duration(v) + " h"
    if kind == "hr":
        return f"{fmt_int(g('hr_min'))} / {fmt_int(g('hr_avg'))} / {fmt_int(g('hr_max'))}"
    if kind == "stress":
        return f"{fmt_float(g('stress_avg'), 0)} / {fmt_int(g('stress_max'))}"
    if kind == "bb":
        return f"{fmt_int(g('body_battery_min'))} / {fmt_int(g('body_battery_max'))}"
    if kind == "spo2":
        return f"{fmt_float(g('spo2_avg'), 0)} / {fmt_int(g('spo2_min'))}"
    return "–"


def detail_lines(row: dict[str, Any]) -> list[str]:
    """Garmin and strap values of one day, side by side."""
    lines = [f"{'':<24}{_('Garmin'):>12}{_('Strap'):>12}"]
    for label, column, kind in DETAIL_FIELDS:
        garmin = _value(row, "garmin", column, kind)
        strap = _value(row, "strap", column, kind)
        if garmin == "–" and strap == "–":
            continue
        lines.append(f"{_(label):<24}{garmin:>12}{strap:>12}")
    lines.append("")
    lines.append(_("Sources: {sources}").format(sources=row.get("sources") or "–"))
    return lines


class DaysScreen(PeriodMixin, WristpullScreen):
    NAME = "days"
    BINDINGS = [
        Binding("left", "shift(-1)", "Earlier", show=False),
        Binding("right", "shift(1)", "Later", show=False),
        Binding("w", "preset('week')", "Week", show=False),
        Binding("m", "preset('month')", "Month", show=False),
        Binding("d", "preset('default')", "Default range", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, Any]] = {}

    def screen_title(self) -> str:
        return _("Days")

    def keys(self) -> list[tuple[str, str]]:
        return self.period_keys() + [("↑↓", _("day")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Vertical(id="days"):
            yield Panel(_("Period"), id="period")
            with Horizontal(classes="row grow"):
                yield DataTable(id="daytable", cursor_type="row", zebra_stripes=True)
                yield Panel(_("Detail"), id="detail")

    def refresh_data(self) -> None:
        start, end = self.ensure_period()
        rows = day_rows(self.app.conn, start, end)
        self.rows = {r["date"]: r for r in rows}
        self.query_one("#period", Panel).set_lines([self.period_label() + "   " + _("{n} days with data").format(n=len(rows))])
        table = self.query_one("#daytable", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Date"), _("Steps"), _("RHR"), _("Sleep"), _("BB"), _("Stress"), _("Int."), _("Strap steps"), _("Src"))
        for r in rows:
            bb = "–" if r.get("garmin_body_battery_max") is None else f"{fmt_int(r['garmin_body_battery_max'])}▸{fmt_int(r['garmin_body_battery_min'])}"
            mod, vig = r.get("garmin_moderate_min"), r.get("garmin_vigorous_min")
            intensity = "–" if mod is None and vig is None else fmt_int((mod or 0) + (vig or 0))
            table.add_row(
                fmt_date(r["date"]),
                fmt_int(r.get("garmin_steps")),
                fmt_int(r.get("garmin_resting_hr")),
                fmt_duration(r.get("garmin_sleep_duration_s")),
                bb,
                fmt_float(r.get("garmin_stress_avg"), 0),
                intensity,
                fmt_int(r.get("strap_steps")),
                "".join(s[0].upper() if s.startswith("garmin") else s[0] for s in (r.get("sources") or "").split("+") if s),
                key=r["date"],
            )
        detail = self.query_one("#detail", Panel)
        if rows:
            self.show_day(rows[-1]["date"])
            table.move_cursor(row=len(rows) - 1)
        else:
            detail.set_lines([_("No data in this period. Import from the watch or pull the phone first.")])

    def show_day(self, day: str) -> None:
        row = self.rows.get(day)
        panel = self.query_one("#detail", Panel)
        if row is None:
            panel.set_lines(["–"])
            return
        panel.border_title = _("Detail") + "  " + fmt_date(day, year=True)
        panel.set_lines(detail_lines(row))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_day(str(event.row_key.value))
