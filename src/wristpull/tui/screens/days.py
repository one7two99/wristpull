"""Screen 3: one row per day from daily_summary (merged Garmin + strap) with a detail pane."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable

from ...detail import DAY_DETAIL_FIELDS, day_detail_rows
from ..format import fmt_date, fmt_duration, fmt_float, fmt_int
from ..i18n import _
from ..queries import day_rows
from ..widgets import Panel
from .base import PeriodMixin, WristpullScreen

DETAIL_FIELDS = DAY_DETAIL_FIELDS  # kept for callers of the old name


def detail_lines(row: dict[str, Any]) -> list[str]:
    """Garmin and strap values of one day, side by side."""
    lines = [f"{'':<24}{_('Garmin'):>12}{_('Strap'):>12}"]
    for cell in day_detail_rows(row):
        label = cell["label"]  # a msgid from detail.DAY_DETAIL_FIELDS (in the catalog), translated here at render time
        lines.append(f"{_(label):<24}{cell['garmin']:>12}{cell['strap']:>12}")
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
