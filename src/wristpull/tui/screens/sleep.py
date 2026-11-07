"""Screen 5: nights of the period, every source, with stage bar and night vitals."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable

from ...detail import local_clock, nights, sleep_night
from ...timeutil import parse_iso_utc, zone
from ..format import fmt_date, fmt_duration, fmt_float, fmt_int, fmt_pct, stage_bar
from ..i18n import _
from ..widgets import Panel
from .base import PeriodMixin, WristpullScreen

SOURCE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("all", ()),
    ("garmin", ("garmin_usb", "gadgetbridge_garmin", "garmin_connect")),
    ("strap", ("zepp_ble", "gadgetbridge_zepp")),
)
BAR_WIDTH = 48


def night_lines(night: dict[str, Any], tz) -> list[str]:
    start, end = parse_iso_utc(night["start_utc"]), parse_iso_utc(night["end_utc"])
    stages = [(parse_iso_utc(s["start_utc"]), parse_iso_utc(s["end_utc"]), s["stage"]) for s in night.get("stages", [])]
    lines = [
        f"{night['source']}   {fmt_date(night['local_date'], year=True)}   {local_clock(night['start_utc'], tz)} – {local_clock(night['end_utc'], tz)}",
        "",
        stage_bar(stages, start, end, BAR_WIDTH),
        f"{local_clock(night['start_utc'], tz):<{BAR_WIDTH - 5}}{local_clock(night['end_utc'], tz)}",
        "▓ " + _("deep") + "  ▒ " + _("light") + "  ░ " + _("REM") + "  · " + _("awake") + "  ␣ " + _("no data"),
        "",
    ]
    total = night.get("duration_s") or 0
    for label, key in ((_("Sleep"), "duration_s"), (_("Deep"), "deep_s"), (_("Light"), "light_s"), (_("REM"), "rem_s"), (_("Awake"), "awake_s"), (_("No data"), "unmeasurable_s")):
        value = night.get(key)
        if value is None or (value == 0 and key in ("unmeasurable_s", "awake_s")):
            continue
        share = f"{fmt_pct(100 * value / total)}" if total and key not in ("duration_s", "awake_s", "unmeasurable_s") else ""
        lines.append(f"{label:<12}{fmt_duration(value) + ' h':>10}  {share}")
    lines.append("")
    lines.append(
        f"{_('Score'):<12}{fmt_int(night.get('score')):>10}   {_('Quality')} {fmt_int(night.get('quality_score'))}   {_('Recovery')} {fmt_int(night.get('recovery_score'))}"
    )
    lines.append(f"{_('Awakenings'):<12}{fmt_int(night.get('awakenings')):>10}   {_('Stress in sleep')} {fmt_float(night.get('avg_stress'), 0)}")
    hrv, spo2, resp, hr = night.get("hrv", {}), night.get("spo2", {}), night.get("respiration", {}), night.get("heart_rate", {})
    lines.append("")
    if hrv.get("n"):
        text = f"{fmt_float(hrv.get('avg'), 0)} ms  ({fmt_int(hrv.get('min'))}–{fmt_int(hrv.get('max'))}, {hrv['n']} {_('values')})"
        if hrv.get("summary"):
            text += f"   {_('night avg')} {fmt_float(hrv['summary'].get('last_night_avg_ms'), 0)} ms, {hrv['summary'].get('status') or ''}"
        lines.append(f"{'HRV':<12}{text}")
    else:
        lines.append(f"{'HRV':<12}–")
    lines.append(f"{'SpO2':<12}" + (f"{fmt_pct(spo2.get('avg'))} {_('avg')}, {fmt_pct(spo2.get('min'))} {_('min')} ({spo2['n']} {_('values')})" if spo2.get("n") else "–"))
    lines.append(f"{_('Breathing'):<12}" + (f"{fmt_float(resp.get('avg'), 1)} /min" if resp.get("n") else "–"))
    lines.append(f"{_('HR'):<12}" + (f"{fmt_int(hr.get('min'))} {_('min')}, {fmt_int(hr.get('avg'))} {_('avg')} ({hr['n']} {_('values')})" if hr.get("n") else "–"))
    return lines


class SleepScreen(PeriodMixin, WristpullScreen):
    NAME = "sleep"
    BINDINGS = [
        Binding("left", "shift(-1)", "Earlier", show=False),
        Binding("right", "shift(1)", "Later", show=False),
        Binding("w", "preset('week')", "Week", show=False),
        Binding("m", "preset('month')", "Month", show=False),
        Binding("d", "preset('default')", "Default range", show=False),
        Binding("g", "toggle_source", "Source", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.group = 0
        self.rows: dict[str, dict[str, Any]] = {}

    def screen_title(self) -> str:
        return _("Sleep")

    def keys(self) -> list[tuple[str, str]]:
        return self.period_keys() + [("g", _("source: {group}").format(group=SOURCE_GROUPS[self.group][0])), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Vertical(id="sleep"):
            yield Panel(_("Period"), id="period")
            with Horizontal(classes="row grow"):
                yield DataTable(id="nights", cursor_type="row", zebra_stripes=True)
                yield Panel(_("Night"), id="nightdetail")

    def action_toggle_source(self) -> None:
        self.group = (self.group + 1) % len(SOURCE_GROUPS)
        self.query_one("#keybar").set_keys(self.keys() + [("/", _("palette")), ("?", _("help")), ("F12", _("language")), ("q", _("quit"))])
        self.refresh_data()

    def refresh_data(self) -> None:
        start, end = self.ensure_period()
        name, sources = SOURCE_GROUPS[self.group]
        rows = nights(self.app.conn, start.isoformat(), end.isoformat())
        if sources:
            rows = [r for r in rows if r["source"] in sources]
        self.rows = {str(r["id"]): r for r in rows}
        self.query_one("#period", Panel).set_lines([self.period_label() + "   " + _("{n} nights").format(n=len(rows)) + f"   {_('source')}: {name}"])
        tz = zone(self.app.config.timezone)
        table = self.query_one("#nights", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Wake-up"), _("Source"), _("Bed"), _("Wake"), _("Sleep"), _("Deep"), _("Light"), _("REM"), _("Awake"), _("Score"))
        for r in rows:
            table.add_row(
                fmt_date(r["local_date"]),
                r["source"].replace("gadgetbridge_", "gb_"),
                local_clock(r["start_utc"], tz),
                local_clock(r["end_utc"], tz),
                fmt_duration(r.get("duration_s")),
                fmt_duration(r.get("deep_s")),
                fmt_duration(r.get("light_s")),
                fmt_duration(r.get("rem_s")),
                fmt_duration(r.get("awake_s")),
                fmt_int(r.get("score")),
                key=str(r["id"]),
            )
        if rows:
            self.show_night(str(rows[0]["id"]))
        else:
            self.query_one("#nightdetail", Panel).set_lines([_("No nights in this period.")])

    def show_night(self, key: str) -> None:
        night = sleep_night(self.app.conn, int(key))
        panel = self.query_one("#nightdetail", Panel)
        if night is None:
            panel.set_lines(["–"])
            return
        panel.set_lines(night_lines(night, zone(self.app.config.timezone)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_night(str(event.row_key.value))
