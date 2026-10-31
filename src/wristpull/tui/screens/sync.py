"""Screen 2: devices & sync - start import/pull jobs with options and watch the log."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label

from ..format import fmt_float
from ..i18n import _
from ..jobs import JobRecord, garmin_import_job, phone_pull_job
from ..widgets import JobLog, Panel
from .base import WristpullScreen


class SyncScreen(WristpullScreen):
    NAME = "sync"
    BINDINGS = [
        Binding("i", "import_garmin", "Import", show=False),
        Binding("p", "pull_phone", "Pull", show=False),
        Binding("l", "clear_log", "Clear log", show=False),
        Binding("c", "copy_cli", "Copy CLI", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def screen_title(self) -> str:
        return _("Devices & Sync")

    def keys(self) -> list[tuple[str, str]]:
        return [("i", _("import")), ("p", _("pull")), ("l", _("clear log")), ("c", _("copy CLI")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        config = self.app.config
        with Horizontal(id="sync"):
            with VerticalScroll(id="forms"):
                with Vertical(classes="form"):
                    yield Label(_("GARMIN (USB)"), classes="form-title")
                    yield Label(_("Path (empty: auto-detect)"))
                    yield Input(placeholder=_("GARMIN folder or mount point"), id="garmin-path")
                    yield Checkbox(_("Dry run (list only)"), id="garmin-dry")
                    yield Button(_("i  Import Garmin"), id="btn-import", variant="primary")
                with Vertical(classes="form"):
                    yield Label(_("PHONE (ADB)"), classes="form-title")
                    yield Label(_("Serial (empty: config or the only device)"))
                    yield Input(value=config.phone_serial or "", id="phone-serial")
                    yield Checkbox(_("Trigger export"), value=True, id="phone-trigger")
                    yield Checkbox(_("Trigger device sync first"), value=True, id="phone-sync")
                    yield Label(_("Wait for export (seconds)"))
                    yield Input(value="60", id="phone-wait", type="number")
                    yield Checkbox(_("Dry run (show only)"), id="phone-dry")
                    yield Button(_("p  Pull phone"), id="btn-pull", variant="primary")
            with Vertical(id="log-side"):
                log = JobLog(self.app.runner.lines, id="log")
                log.border_title = _("Log")
                yield log
                yield Panel(_("Last result"), id="result")

    def refresh_data(self) -> None:
        self._show_last(self.app.runner.last)
        busy = self.app.runner.busy
        self.query_one("#btn-import", Button).disabled = busy
        self.query_one("#btn-pull", Button).disabled = busy

    def _show_last(self, record: JobRecord | None) -> None:
        panel = self.query_one("#result", Panel)
        if record is None:
            panel.set_lines([_("No job run yet.")])
            return
        lines = [f"{record.spec.title}  ·  {fmt_float(record.duration_s, 1)} s", *record.lines, "", f"$ {record.spec.cli}"]
        panel.set_lines(lines)

    def job_line(self, text: str) -> None:
        self.query_one("#log", JobLog).append(text)

    def job_finished(self, record: JobRecord) -> None:
        for line in record.lines:
            self.query_one("#log", JobLog).append(line)
        self.refresh_data()

    def _garmin_spec(self):
        path_text = self.query_one("#garmin-path", Input).value.strip()
        dry = self.query_one("#garmin-dry", Checkbox).value
        return garmin_import_job(self.app.config, self.app.deps, path=Path(path_text).expanduser() if path_text else None, dry_run=dry)

    def _phone_spec(self):
        serial = self.query_one("#phone-serial", Input).value.strip() or None
        trigger = self.query_one("#phone-trigger", Checkbox).value
        sync = self.query_one("#phone-sync", Checkbox).value
        dry = self.query_one("#phone-dry", Checkbox).value
        try:
            wait = float(self.query_one("#phone-wait", Input).value or 60)
        except ValueError:
            wait = 60.0
        return phone_pull_job(self.app.config, self.app.deps, serial=serial, trigger=trigger, sync=sync, wait=wait, dry_run=dry)

    def action_import_garmin(self) -> None:
        self.app.start_job(self._garmin_spec())
        self.refresh_data()

    def action_pull_phone(self) -> None:
        self.app.start_job(self._phone_spec())
        self.refresh_data()

    def action_clear_log(self) -> None:
        self.app.runner.lines.clear()
        self.query_one("#log", JobLog).clear()

    def action_copy_cli(self) -> None:
        record = self.app.runner.last
        spec = self.app.runner.active or (record.spec if record else None)
        if spec is None:
            self.app.notify(_("No job yet"), severity="warning")
            return
        self.app.copy_to_clipboard(spec.cli)
        self.app.notify(_("Copied: {cli}").format(cli=spec.cli))

    @on(Button.Pressed, "#btn-import")
    def _on_import(self) -> None:
        self.action_import_garmin()

    @on(Button.Pressed, "#btn-pull")
    def _on_pull(self) -> None:
        self.action_pull_phone()
