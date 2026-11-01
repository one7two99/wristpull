"""Screen 7: export for Claude or weekly/monthly report, with result list and Markdown preview."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet

from ...aggregate import month_bounds, parse_date, week_bounds
from ...export import ExportResult
from ...report import ReportResult
from ..format import fmt_bytes
from ..i18n import _
from ..jobs import JobRecord, export_job, last_days, report_job
from ..widgets import Panel
from .base import WristpullScreen

PREVIEW_LINES = 80


class ExportScreen(WristpullScreen):
    NAME = "export"
    BINDINGS = [
        Binding("e", "run", "Generate", show=False),
        Binding("o", "open_dir", "Open folder", show=False),
        Binding("y", "copy_path", "Copy path", show=False),
        Binding("c", "copy_cli", "Copy CLI", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.last_dir: Path | None = None
        self.last_cli: str | None = None

    def screen_title(self) -> str:
        return _("Export & Report")

    def keys(self) -> list[tuple[str, str]]:
        return [("e", _("generate")), ("o", _("open folder")), ("y", _("copy path")), ("c", _("copy CLI")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        start, end = last_days(self.app.deps.today(), self.app.config.ui_range_days)
        with Horizontal(id="export"):
            with VerticalScroll(id="exportform"):
                yield Label(_("WHAT"), classes="form-title")
                with RadioSet(id="kind"):
                    yield RadioButton(_("Claude export (Markdown + CSV)"), id="kind-export", value=True)
                    yield RadioButton(_("Weekly report"), id="kind-week")
                    yield RadioButton(_("Monthly report"), id="kind-month")
                yield Label(_("PERIOD"), classes="form-title")
                yield Label(_("From (YYYY-MM-DD); reports use the week/month of 'To'"))
                yield Input(value=start.isoformat(), id="exp-from")
                yield Label(_("To"))
                yield Input(value=end.isoformat(), id="exp-to")
                with Horizontal(classes="row"):
                    yield Button(_("7 days"), id="preset-7", classes="small")
                    yield Button(_("default"), id="preset-default", classes="small")
                    yield Button(_("this week"), id="preset-week", classes="small")
                    yield Button(_("this month"), id="preset-month", classes="small")
                yield Label(_("OPTIONS"), classes="form-title")
                yield Checkbox(_("Write CSV files (export)"), value=True, id="exp-csv")
                yield Checkbox(_("Charts as SVG (reports)"), value=True, id="exp-charts")
                yield Button(_("e  Generate"), id="btn-export", variant="primary")
            with Vertical(id="export-side"):
                yield Panel(_("Result"), id="export-result")
                yield Panel(_("Preview"), id="export-preview")

    def refresh_data(self) -> None:
        record = self.app.runner.last
        if record is not None and isinstance(record.result, (ExportResult, ReportResult)):
            self._show(record)
        elif self.last_dir is None:
            self.query_one("#export-result", Panel).set_lines([_("Nothing generated yet. Choose what and when, then press e.")])
            self.query_one("#export-preview", Panel).set_lines([])
        self.query_one("#btn-export", Button).disabled = self.app.runner.busy

    def _kind(self) -> str:
        pressed = self.query_one("#kind", RadioSet).pressed_button
        return (pressed.id or "kind-export").removeprefix("kind-") if pressed else "export"

    def _dates(self) -> tuple[date, date] | None:
        try:
            start = parse_date(self.query_one("#exp-from", Input).value.strip())
            end = parse_date(self.query_one("#exp-to", Input).value.strip())
        except ValueError:
            self.app.notify(_("Dates must be YYYY-MM-DD"), severity="error")
            return None
        if end < start:
            self.app.notify(_("'To' must not be before 'From'"), severity="error")
            return None
        return start, end

    def build_spec(self):
        dates = self._dates()
        if dates is None:
            return None
        start, end = dates
        kind = self._kind()
        if kind == "export":
            return export_job(self.app.config, start, end, write_csv=self.query_one("#exp-csv", Checkbox).value)
        return report_job(self.app.config, kind, end, charts=self.query_one("#exp-charts", Checkbox).value)

    def action_run(self) -> None:
        spec = self.build_spec()
        if spec is None:
            return
        self.last_cli = spec.cli
        self.app.start_job(spec)
        self.refresh_data()

    def _set_dates(self, start: date, end: date) -> None:
        self.query_one("#exp-from", Input).value = start.isoformat()
        self.query_one("#exp-to", Input).value = end.isoformat()

    @on(Button.Pressed, ".small")
    def _preset(self, event: Button.Pressed) -> None:
        today = self.app.deps.today()
        which = (event.button.id or "").removeprefix("preset-")
        if which == "7":
            self._set_dates(today - timedelta(days=6), today)
        elif which == "week":
            self._set_dates(*week_bounds(today))
        elif which == "month":
            self._set_dates(*month_bounds(today))
        else:
            self._set_dates(*last_days(today, self.app.config.ui_range_days))

    @on(Button.Pressed, "#btn-export")
    def _run_pressed(self) -> None:
        self.action_run()

    def job_finished(self, record: JobRecord) -> None:
        if isinstance(record.result, (ExportResult, ReportResult)):
            self._show(record)
        self.refresh_data()

    def _show(self, record: JobRecord) -> None:
        result = record.result
        self.last_dir = Path(result.directory)
        self.last_cli = record.spec.cli
        lines = [str(result.directory), ""]
        for path in result.files:
            try:
                size = fmt_bytes(Path(path).stat().st_size)
            except OSError:
                size = "–"
            lines.append(f"  {Path(path).name:<28}{size:>10}")
        lines += ["", f"$ {record.spec.cli}"]
        self.query_one("#export-result", Panel).set_lines(lines)
        preview = self.query_one("#export-preview", Panel)
        text = result.markdown.splitlines()
        preview.border_title = _("Preview") + (f"  ({len(text)} {_('lines')})" if text else "")
        preview.set_lines(text[:PREVIEW_LINES] + ([_("…")] if len(text) > PREVIEW_LINES else []))

    def action_open_dir(self) -> None:
        if self.last_dir is None:
            self.app.notify(_("Nothing generated yet"), severity="warning")
            return
        self.app.deps.opened.append(self.last_dir)
        self.app.deps.open_path(self.last_dir)
        self.app.notify(_("Opened {path}").format(path=self.last_dir.name))

    def action_copy_path(self) -> None:
        if self.last_dir is None:
            self.app.notify(_("Nothing generated yet"), severity="warning")
            return
        self.app.copy_to_clipboard(str(self.last_dir))
        self.app.notify(_("Copied path"))

    def action_copy_cli(self) -> None:
        spec = self.build_spec()
        cli = spec.cli if spec else self.last_cli
        if not cli:
            return
        self.app.copy_to_clipboard(cli)
        self.app.notify(_("Copied: {cli}").format(cli=cli))
