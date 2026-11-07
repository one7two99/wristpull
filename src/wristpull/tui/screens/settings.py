"""Screen 8: edit the configuration and write it back to config.toml."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select

from ...config import COACH_BACKENDS, COACH_EFFORTS, UI_LANGUAGES, CoachConfig, Config, default_config_path, validate_config, write_config
from ..i18n import _, current_language, set_language
from ..widgets import Panel
from .base import WristpullScreen


def _list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


class SettingsScreen(WristpullScreen):
    NAME = "config"
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("ctrl+r", "reset", "Reset", show=False),
        Binding("ctrl+t", "check_coach", "Check coach backend", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def screen_title(self) -> str:
        return _("Settings")

    def keys(self) -> list[tuple[str, str]]:
        return [("Ctrl+S", _("save")), ("Ctrl+R", _("reset")), ("Ctrl+T", _("check coach backend")), ("Tab", _("next field")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        config = self.app.config
        with Horizontal(id="settings"):
            with VerticalScroll(id="settingsform"):
                yield Label(_("STORAGE"), classes="form-title")
                yield Label("data_dir  " + _("(takes effect after a restart)"))
                yield Input(value=str(config.file_data_dir or config.data_dir), id="set-data-dir")
                yield Label("timezone")
                yield Input(value=config.timezone, id="set-timezone")
                yield Label(_("GARMIN (USB)"), classes="form-title")
                yield Label(_("folders, comma-separated"))
                yield Input(value=", ".join(config.garmin_folders), id="set-folders")
                yield Label(_("mount hints, comma-separated"))
                yield Input(value=", ".join(str(p) for p in config.garmin_mount_hints), id="set-hints")
                yield Label(_("PHONE (ADB)"), classes="form-title")
                yield Label("serial")
                yield Input(value=config.phone_serial or "", id="set-serial")
                yield Label("export_dir")
                yield Input(value=config.phone_export_dir, id="set-export-dir")
                yield Label(_("INTERFACE"), classes="form-title")
                yield Label(_("language"))
                yield Select([(_("system"), ""), ("English", "en"), ("Deutsch", "de")], value=config.ui_language or "", allow_blank=False, id="set-language")
                yield Label(_("theme"))
                yield Input(value=config.ui_theme, id="set-theme")
                yield Label(_("default range (days)"))
                yield Input(value=str(config.ui_range_days), id="set-range", type="integer")
                coach = config.coach
                yield Label(_("COACH (LLM)"), classes="form-title")
                yield Label(_("backend"))
                yield Select([(name, name) for name in COACH_BACKENDS], value=coach.backend, allow_blank=False, id="set-coach-backend")
                yield Label(_("model"))
                yield Input(value=coach.model, id="set-coach-model")
                yield Label(_("weekly model (empty: same as model)"))
                yield Input(value=coach.weekly_model or "", id="set-coach-weekly-model")
                yield Label(_("API key environment variable (empty: ANTHROPIC_API_KEY / OPENROUTER_API_KEY)"))
                yield Input(value=coach.api_key_env or "", id="set-coach-key-env")
                yield Label("ollama_url")
                yield Input(value=coach.ollama_url, id="set-coach-ollama-url")
                yield Label(_("Ollama context window (num_ctx)"))
                yield Input(value=str(coach.num_ctx), id="set-coach-num-ctx", type="integer")
                yield Label(_("max answer tokens"))
                yield Input(value=str(coach.max_tokens), id="set-coach-max-tokens", type="integer")
                yield Label(_("timeout (seconds)"))
                yield Input(value=f"{coach.timeout_s:g}", id="set-coach-timeout", type="number")
                yield Label(_("data package budget (tokens)"))
                yield Input(value=str(coach.token_budget), id="set-coach-budget", type="integer")
                yield Label(_("effort (Claude only)"))
                yield Select([(name, name) for name in COACH_EFFORTS], value=coach.effort, allow_blank=False, id="set-coach-effort")
                yield Label(_("answer language"))
                yield Select([(_("UI language"), ""), ("English", "en"), ("Deutsch", "de")], value=coach.language or "", allow_blank=False, id="set-coach-language")
                with Horizontal(classes="row"):
                    yield Button(_("Save"), id="btn-save", variant="primary")
                    yield Button(_("Reset"), id="btn-reset")
                    yield Button(_("Check coach"), id="btn-check-coach")
            with Vertical(id="settings-side"):
                yield Panel(_("Configuration file"), id="settings-status")
                yield Panel(_("Preview (TOML)"), id="settings-preview")

    def refresh_data(self) -> None:
        config = self.app.config
        path = config.path or default_config_path()
        self.query_one("#settings-status", Panel).set_lines([f"{path}  ({_('exists') if path.exists() else _('missing, showing defaults')})"])
        self.query_one("#settings-preview", Panel).set_lines(config.render().rstrip().splitlines())

    def _int(self, selector: str, default: int = 0) -> int:
        try:
            return int(self.query_one(selector, Input).value or default)
        except ValueError:
            return default

    def collect_coach(self) -> CoachConfig:
        try:
            timeout = float(self.query_one("#set-coach-timeout", Input).value or 0)
        except ValueError:
            timeout = 0.0
        coach_language = self.query_one("#set-coach-language", Select).value
        return replace(
            self.app.config.coach,
            backend=str(self.query_one("#set-coach-backend", Select).value),
            model=self.query_one("#set-coach-model", Input).value.strip(),
            weekly_model=self.query_one("#set-coach-weekly-model", Input).value.strip() or None,
            api_key_env=self.query_one("#set-coach-key-env", Input).value.strip() or None,
            ollama_url=self.query_one("#set-coach-ollama-url", Input).value.strip() or "http://localhost:11434",
            num_ctx=self._int("#set-coach-num-ctx"),
            max_tokens=self._int("#set-coach-max-tokens"),
            timeout_s=timeout,
            token_budget=self._int("#set-coach-budget"),
            effort=str(self.query_one("#set-coach-effort", Select).value),
            language=str(coach_language) if coach_language else None,
        )

    def collect(self) -> Config:
        """A Config built from the form (not yet validated)."""
        language = self.query_one("#set-language", Select).value
        range_days = self._int("#set-range")
        data_dir = Path(self.query_one("#set-data-dir", Input).value.strip()).expanduser()
        overridden = self.app.config.file_data_dir is not None  # WRISTPULL_DATA_DIR: the form edits the file's value
        return replace(
            self.app.config,
            data_dir=self.app.config.data_dir if overridden else data_dir,
            file_data_dir=data_dir if overridden else None,
            timezone=self.query_one("#set-timezone", Input).value.strip(),
            garmin_folders=_list(self.query_one("#set-folders", Input).value),
            garmin_mount_hints=[Path(p).expanduser() for p in _list(self.query_one("#set-hints", Input).value)],
            phone_serial=self.query_one("#set-serial", Input).value.strip() or None,
            phone_export_dir=self.query_one("#set-export-dir", Input).value.strip(),
            ui_language=str(language) if language else None,
            ui_theme=self.query_one("#set-theme", Input).value.strip() or "textual-dark",
            ui_range_days=range_days,
            coach=self.collect_coach(),
        )

    def action_save(self) -> None:
        cfg = self.collect()
        problems = validate_config(cfg)
        if cfg.ui_theme not in self.app.available_themes:
            problems.append(_("unknown theme: {theme}").format(theme=cfg.ui_theme))
        if problems:
            self.query_one("#settings-status", Panel).set_lines(["✗ " + p for p in problems])
            self.app.notify(_("Not saved: {n} problems").format(n=len(problems)), severity="error")
            return
        path = write_config(cfg, self.app.config.path or default_config_path())
        live = self.app.config
        for field_name in ("data_dir", "file_data_dir", "timezone", "garmin_folders", "garmin_mount_hints", "phone_serial", "phone_export_dir", "ui_language", "ui_theme", "ui_range_days", "coach", "path"):
            setattr(live, field_name, getattr(cfg, field_name))
        self.app.theme = cfg.ui_theme
        self.app.notify(_("Saved to {path}").format(path=path))
        wanted = cfg.ui_language or current_language()
        if wanted in UI_LANGUAGES and wanted != current_language():
            self.app.language = set_language(wanted)
            self.app.switch_screen(SettingsScreen())
            return
        self.refresh_data()

    def action_reset(self) -> None:
        self.app.switch_screen(SettingsScreen())

    def action_check_coach(self) -> None:
        """Ask the configured backend whether it is reachable, as a background job (Ollama may need a moment)."""
        from ...coach.backends import CoachError, make_backend
        from ..jobs import JobSpec

        cfg = replace(self.app.config, coach=self.collect_coach())
        problems = validate_config(cfg)
        if problems:
            self.app.notify(_("Not checked: {n} problems").format(n=len(problems)), severity="error")
            return

        def run(conn, log):
            factory = self.app.deps.coach_backend or (lambda c, **kw: make_backend(c, **kw))
            try:
                backend = factory(cfg)
                return backend.check()
            except CoachError as exc:
                raise RuntimeError(str(exc)) from exc

        self.app.start_job(JobSpec("coach_check", _("Coach backend check"), f"wristpull coach daily --dry-run  # backend {cfg.coach.backend}/{cfg.coach.model}", run))

    def job_finished(self, record) -> None:
        if record.spec.key == "coach_check":
            text = str(record.result) if record.ok else str(record.error)
            self.query_one("#settings-status", Panel).set_lines([("✓ " if record.ok else "✗ ") + text])
            return
        self.refresh_data()

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#btn-reset")
    def _reset(self) -> None:
        self.action_reset()

    @on(Button.Pressed, "#btn-check-coach")
    def _check(self) -> None:
        self.action_check_coach()
