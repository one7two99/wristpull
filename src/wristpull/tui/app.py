"""The Textual application: screens, global keys, job orchestration."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.worker import Worker

from .. import __version__
from ..config import Config
from ..db import connect
from .deps import TuiDeps
from .i18n import _, next_language, resolve_language, set_language
from .jobs import JobLine, JobRunner, JobSpec, export_job, garmin_import_job, last_days, phone_pull_job, report_job
from .screens.activities import ActivitiesScreen
from .screens.days import DaysScreen
from .screens.export import ExportScreen
from .screens.files import FilesScreen
from .screens.help import HelpScreen
from .screens.home import HomeScreen
from .screens.settings import SettingsScreen
from .screens.sleep import SleepScreen
from .screens.sync import SyncScreen

SCREEN_FACTORIES = {
    "home": HomeScreen,
    "sync": SyncScreen,
    "days": DaysScreen,
    "activities": ActivitiesScreen,
    "sleep": SleepScreen,
    "files": FilesScreen,
    "export": ExportScreen,
    "config": SettingsScreen,
}
SCREEN_KEYS = {"1": "home", "2": "sync", "3": "days", "4": "activities", "5": "sleep", "6": "files", "7": "export", "8": "config"}


class WristpullApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = f"wristpull {__version__}"
    BINDINGS = [
        Binding("1", "goto('home')", "Overview", show=False),
        Binding("2", "goto('sync')", "Devices & Sync", show=False),
        Binding("3", "goto('days')", "Days", show=False),
        Binding("4", "goto('activities')", "Activities", show=False),
        Binding("5", "goto('sleep')", "Sleep", show=False),
        Binding("6", "goto('files')", "Files", show=False),
        Binding("7", "goto('export')", "Export & Report", show=False),
        Binding("8", "goto('config')", "Settings", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("f12", "toggle_language", "Language", show=False),
        Binding("slash", "command_palette", "Palette", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, config: Config, deps: TuiDeps | None = None) -> None:
        super().__init__()
        self.config = config
        self.deps = deps or TuiDeps()
        self.language = set_language(resolve_language(config.ui_language))
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.conn = connect(config.db_path)
        self.runner = JobRunner(self, config.db_path)

    # -- lifecycle ----------------------------------------------------------------------

    def on_mount(self) -> None:
        if self.config.ui_theme in self.available_themes:
            self.theme = self.config.ui_theme
        self.push_screen(HomeScreen())
        if self.deps.refresh_interval > 0:
            self.set_interval(self.deps.refresh_interval, self._poll)

    def _poll(self) -> None:
        if isinstance(self.screen, HomeScreen) and not self.runner.busy:
            self.screen.refresh_data()

    def on_unmount(self) -> None:
        self.conn.close()

    # -- navigation ---------------------------------------------------------------------

    def action_goto(self, name: str) -> None:
        factory = SCREEN_FACTORIES[name]
        if isinstance(self.screen, factory):
            return
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        self.switch_screen(factory())

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_toggle_language(self) -> None:
        self.language = set_language(next_language())
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        current = type(self.screen)
        if current in SCREEN_FACTORIES.values():
            self.switch_screen(current())
        self.notify(_("Language: {lang}").format(lang=self.language))

    # -- jobs ---------------------------------------------------------------------------

    def start_job(self, spec: JobSpec) -> bool:
        if not self.runner.start(spec):
            active = self.runner.active
            self.notify(_("A job is already running: {title}").format(title=active.title if active else ""), severity="warning")
            return False
        self.notify(_("Started: {title}").format(title=spec.title))
        self._screen_hook("job_line", self.runner.lines[-1])
        return True

    def action_import_garmin(self) -> None:
        self.start_job(garmin_import_job(self.config, self.deps, stop=self.runner.should_stop))

    def action_pull_phone(self) -> None:
        self.start_job(phone_pull_job(self.config, self.deps, stop=self.runner.should_stop))

    def action_cancel_job(self) -> None:
        if self.runner.cancel():
            self.notify(_("Stop requested; the job ends after the current file"), severity="warning")
            self._screen_hook("job_line", self.runner.lines[-1])
        else:
            self.notify(_("No job is running"))

    def action_export(self) -> None:
        start, end = last_days(self.deps.today(), self.config.ui_range_days)
        self.start_job(export_job(self.config, start, end))

    def action_report(self) -> None:
        self.start_job(report_job(self.config, "week", self.deps.today()))

    def on_job_line(self, message: JobLine) -> None:
        self.runner.on_line(message.text)
        self._screen_hook("job_line", message.text)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "job":
            return
        record = self.runner.finish(event.worker)
        if record is None:
            return
        severity = "information" if record.ok else "error"
        self.notify(record.lines[0] if record.lines else record.spec.title, title=record.spec.title, severity=severity, timeout=8)
        self._screen_hook("job_finished", record)

    def _screen_hook(self, name: str, *args) -> None:
        hook = getattr(self.screen, name, None)
        if callable(hook):
            hook(*args)


def run_tui(config: Config, deps: TuiDeps | None = None) -> int:
    WristpullApp(config, deps).run()
    return 0
