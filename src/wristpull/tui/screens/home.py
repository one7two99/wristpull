"""Screen 1: overview of devices, last import, 7-day KPIs, recent activities, notes."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable

from ..format import fmt_bytes, fmt_clock, fmt_date, fmt_datetime, fmt_int, fmt_km
from ..i18n import _
from ..queries import KPI_KEYS, Kpi, Overview, load_overview
from ..widgets import KpiTile, Panel
from .base import WristpullScreen


class HomeScreen(WristpullScreen):
    NAME = "home"
    BINDINGS = [
        Binding("i", "app.import_garmin", "Import Garmin", show=False),
        Binding("p", "app.pull_phone", "Pull phone", show=False),
        Binding("s", "refresh", "Refresh", show=False),
        Binding("e", "app.export", "Export", show=False),
        Binding("r", "app.report", "Report", show=False),
    ]

    def screen_title(self) -> str:
        return _("Overview")

    def keys(self) -> list[tuple[str, str]]:
        return [("i", _("import Garmin")), ("p", _("pull phone")), ("s", _("refresh")), ("e", _("export")), ("r", _("weekly report"))]

    def body(self) -> ComposeResult:
        with Vertical(id="home"):
            with Horizontal(classes="row"):
                yield Panel(_("Devices"), id="devices")
                yield Panel(_("Data store"), id="store")
            yield Panel(_("Last 7 days"), id="kpi-title")
            with Horizontal(id="kpis", classes="row"):
                for key in KPI_KEYS:
                    yield KpiTile(Kpi(key, "", "", "", ""), id=f"kpi-{key}")
            with Horizontal(classes="row grow"):
                table = DataTable(id="recent", cursor_type="row", zebra_stripes=True)
                table.border_title = _("Recent activities")
                yield table
                yield Panel(_("Notes"), id="notes")

    def action_refresh(self) -> None:
        self.refresh_data()
        self.app.notify(_("Refreshed"))

    def refresh_data(self) -> None:
        app = self.app
        overview = load_overview(app.conn, app.config, app.deps.today(), deps=app.deps)
        self._show_overview(overview)

    def _show_overview(self, ov: Overview) -> None:
        status = ov.status
        devices: list[str] = []
        if status["garmin_connected"]:
            model = (status.get("garmin_device") or {}).get("model") or "Garmin"
            devices.append(f"● {model}: " + _("USB, mounted at {path}").format(path=status["garmin_root"]))
        elif status.get("garmin_unmounted"):
            devices.append("○ Garmin: " + _("attached, not mounted"))
        else:
            devices.append("✕ Garmin: " + _("not connected"))
        serial = self.app.config.phone_serial
        devices.append("○ " + _("Phone (ADB): {serial}").format(serial=serial or _("first device")) + " · " + _("checked when pulling"))
        self.query_one("#devices", Panel).set_lines(devices)

        store = [
            _("database {size}").format(size=fmt_bytes(int(status["db_size_bytes"]))),
            _("last import {when}").format(when=fmt_datetime(status["last_import"])),
        ]
        for entry in status["raw_files"]:
            err = f"  ({entry['errors']} {_('errors')})" if entry["errors"] else ""
            store.append(f"{entry['source']:<20} {entry['kind']:<9} {fmt_int(entry['count']):>6}{err}")
        self.query_one("#store", Panel).set_lines(store)

        self.query_one("#kpi-title", Panel).border_title = _("Last 7 days") + f"  {fmt_date(ov.start)} – {fmt_date(ov.end)}"
        self.query_one("#kpi-title", Panel).update("")
        for kpi in ov.kpis:
            self.query_one(f"#kpi-{kpi.key}", KpiTile).show(kpi)

        table = self.query_one("#recent", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Date"), _("Sport"), _("Distance"), _("Duration"), _("avg HR"), _("Source"))
        for act in ov.recent:
            table.add_row(
                fmt_date(act.get("local_date")),
                act.get("sport") or act.get("profile_name") or "–",
                fmt_km(act.get("distance_m")),
                fmt_clock(act.get("timer_s") or act.get("elapsed_s")),
                fmt_int(act.get("avg_hr")),
                act.get("source") or "",
                key=str(act.get("id")),
            )
        self.query_one("#notes", Panel).set_lines(ov.notes)
