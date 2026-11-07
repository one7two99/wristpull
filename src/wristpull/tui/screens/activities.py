"""Screen 4: activities of the period with a detail pane (laps, HR zones, HR curve)."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input

from ...detail import activities, activity_detail, downsample, local_clock
from ...timeutil import zone
from ..format import bar, fmt_clock, fmt_date, fmt_float, fmt_int, fmt_km, sparkline
from ..i18n import _
from ..widgets import Panel
from .base import PeriodMixin, WristpullScreen


def pace_per_km(distance_m: float | None, seconds: float | None) -> str:
    if not distance_m or not seconds or distance_m < 100:
        return "–"
    return fmt_clock(seconds / (distance_m / 1000)) + " /km"


def detail_lines(detail: dict[str, Any], tz) -> list[str]:
    start, end = detail["start_utc"], detail.get("end_utc")
    head = f"{detail.get('sport') or '–'}"
    if detail.get("name") or detail.get("profile_name"):
        head += f" · {detail.get('name') or detail['profile_name']}"
    lines = [head, f"{fmt_date(detail['local_date'], year=True)}  {local_clock(start, tz)}" + (f"–{local_clock(end, tz)}" if end else "") + f"   {detail['source']}", ""]
    duration = detail.get("timer_s") or detail.get("elapsed_s")
    lines.append(f"{_('Duration'):<14}{fmt_clock(duration):>12}   {_('Distance'):<10}{fmt_km(detail.get('distance_m')):>10}")
    lines.append(f"{_('Pace'):<14}{pace_per_km(detail.get('distance_m'), duration):>12}   {_('Calories'):<10}{fmt_int(detail.get('calories')):>10}")
    lines.append(f"{_('HR avg / max'):<14}{fmt_int(detail.get('avg_hr')) + ' / ' + fmt_int(detail.get('max_hr')):>12}   {_('Ascent'):<10}{(fmt_int(detail.get('ascent_m')) + ' m') if detail.get('ascent_m') is not None else '–':>10}")
    te = f"{fmt_float(detail.get('aerobic_te'), 1)} / {fmt_float(detail.get('anaerobic_te'), 1)}"
    lines.append(f"{_('Training effect'):<14}{te:>12}   {_('Load'):<10}{fmt_float(detail.get('training_load'), 0):>10}")
    if detail.get("avg_cadence"):
        lines.append(f"{_('Cadence avg'):<14}{fmt_float(detail.get('avg_cadence'), 0):>12}")
    zones = detail.get("hr_zones_s") or []
    if any(zones):
        lines += ["", _("Time in HR zones")]
        top = max(zones)
        for i, seconds in enumerate(zones):
            lines.append(f"Z{i + 1} {bar(seconds, top, 24):<24} {fmt_clock(seconds)}")
    hr = [r.get("hr") for r in detail.get("records", [])]
    present = [h for h in hr if h is not None]
    if present:
        lines += ["", _("Heart rate over time") + f"  ({fmt_int(min(present))}–{fmt_int(max(present))} bpm)", sparkline(downsample(hr, 48))]
    laps = detail.get("laps") or []
    if laps:
        lines += ["", f"{_('Lap'):<5}{_('Distance'):>10}{_('Time'):>10}{_('avg HR'):>8}"]
        for lap in laps[:12]:
            lines.append(f"{lap['lap_index'] + 1:<5}{fmt_km(lap.get('distance_m')):>10}{fmt_clock(lap.get('timer_s') or lap.get('elapsed_s')):>10}{fmt_int(lap.get('avg_hr')):>8}")
        if len(laps) > 12:
            lines.append(_("… {n} laps in total").format(n=len(laps)))
    if detail.get("archive_path"):
        lines += ["", _("File") + f"  {detail['archive_path']}"]
    return lines


class ActivitiesScreen(PeriodMixin, WristpullScreen):
    NAME = "activities"
    BINDINGS = [
        Binding("left", "shift(-1)", "Earlier", show=False),
        Binding("right", "shift(1)", "Later", show=False),
        Binding("w", "preset('week')", "Week", show=False),
        Binding("m", "preset('month')", "Month", show=False),
        Binding("d", "preset('default')", "Default range", show=False),
        Binding("f", "focus_filter", "Filter", show=False),
        Binding("o", "open_file", "Open file", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, Any]] = {}
        self.current: dict[str, Any] | None = None

    def screen_title(self) -> str:
        return _("Activities")

    def keys(self) -> list[tuple[str, str]]:
        return self.period_keys() + [("f", _("filter")), ("o", _("open file")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Vertical(id="activities"):
            with Horizontal(id="act-top", classes="row"):
                yield Panel(_("Period"), id="period")
                yield Input(placeholder=_("filter: sport or name"), id="filter")
            with Horizontal(classes="row grow"):
                yield DataTable(id="acttable", cursor_type="row", zebra_stripes=True)
                yield Panel(_("Detail"), id="actdetail")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#acttable", DataTable).focus()  # keys go to the table, not into the filter field

    def refresh_data(self) -> None:
        start, end = self.ensure_period()
        needle = self.query_one("#filter", Input).value.strip() or None
        rows = activities(self.app.conn, start.isoformat(), end.isoformat(), sport=needle)
        self.rows = {str(r["id"]): r for r in rows}
        self.query_one("#period", Panel).set_lines([self.period_label() + "   " + _("{n} activities").format(n=len(rows))])
        tz = zone(self.app.config.timezone)
        table = self.query_one("#acttable", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Date"), _("Time"), _("Sport"), _("Name"), _("Duration"), _("Distance"), _("avg HR"), _("max HR"), _("kcal"), _("Source"))
        for r in rows:
            table.add_row(
                fmt_date(r["local_date"]),
                local_clock(r["start_utc"], tz),
                r.get("sport") or "–",
                (r.get("name") or r.get("profile_name") or "")[:18],
                fmt_clock(r.get("timer_s") or r.get("elapsed_s")),
                fmt_km(r.get("distance_m")),
                fmt_int(r.get("avg_hr")),
                fmt_int(r.get("max_hr")),
                fmt_int(r.get("calories")),
                r["source"],
                key=str(r["id"]),
            )
        if rows:
            self.show_activity(str(rows[0]["id"]))
        else:
            self.current = None
            self.query_one("#actdetail", Panel).set_lines([_("No activities in this period.")])

    def show_activity(self, key: str) -> None:
        detail = activity_detail(self.app.conn, int(key))
        self.current = detail
        panel = self.query_one("#actdetail", Panel)
        if detail is None:
            panel.set_lines(["–"])
            return
        panel.border_title = _("Detail")
        panel.set_lines(detail_lines(detail, zone(self.app.config.timezone)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_activity(str(event.row_key.value))

    @on(Input.Changed, "#filter")
    def _filter_changed(self) -> None:
        self.refresh_data()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_open_file(self) -> None:
        if not self.current or not self.current.get("archive_path"):
            self.app.notify(_("No raw file for this activity"), severity="warning")
            return
        path = self.app.config.raw_dir / self.current["archive_path"]
        self.app.deps.opened.append(path)
        message = self.app.deps.open_path(path)
        self.app.notify(message or _("Opened {path}").format(path=path.name))
