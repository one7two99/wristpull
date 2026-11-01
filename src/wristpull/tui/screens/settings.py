"""Screen 8: edit the configuration and write it back to config.toml."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Select

from ...config import UI_LANGUAGES, Config, default_config_path, validate_config, write_config
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
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def screen_title(self) -> str:
        return _("Settings")

    def keys(self) -> list[tuple[str, str]]:
        return [("Ctrl+S", _("save")), ("Ctrl+R", _("reset")), ("Tab", _("next field")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        config = self.app.config
        with Horizontal(id="settings"):
            with VerticalScroll(id="settingsform"):
                yield Label(_("STORAGE"), classes="form-title")
                yield Label("data_dir  " + _("(takes effect after a restart)"))
                yield Input(value=str(config.data_dir), id="set-data-dir")
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
                with Horizontal(classes="row"):
                    yield Button(_("Save"), id="btn-save", variant="primary")
                    yield Button(_("Reset"), id="btn-reset")
            with Vertical(id="settings-side"):
                yield Panel(_("Configuration file"), id="settings-status")
                yield Panel(_("Preview (TOML)"), id="settings-preview")

    def refresh_data(self) -> None:
        config = self.app.config
        path = config.path or default_config_path()
        self.query_one("#settings-status", Panel).set_lines([f"{path}  ({_('exists') if path.exists() else _('missing, showing defaults')})"])
        self.query_one("#settings-preview", Panel).set_lines(config.render().rstrip().splitlines())

    def collect(self) -> Config:
        """A Config built from the form (not yet validated)."""
        language = self.query_one("#set-language", Select).value
        try:
            range_days = int(self.query_one("#set-range", Input).value or 0)
        except ValueError:
            range_days = 0
        return replace(
            self.app.config,
            data_dir=Path(self.query_one("#set-data-dir", Input).value.strip()).expanduser(),
            timezone=self.query_one("#set-timezone", Input).value.strip(),
            garmin_folders=_list(self.query_one("#set-folders", Input).value),
            garmin_mount_hints=[Path(p).expanduser() for p in _list(self.query_one("#set-hints", Input).value)],
            phone_serial=self.query_one("#set-serial", Input).value.strip() or None,
            phone_export_dir=self.query_one("#set-export-dir", Input).value.strip(),
            ui_language=str(language) if language else None,
            ui_theme=self.query_one("#set-theme", Input).value.strip() or "textual-dark",
            ui_range_days=range_days,
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
        for field_name in ("data_dir", "timezone", "garmin_folders", "garmin_mount_hints", "phone_serial", "phone_export_dir", "ui_language", "ui_theme", "ui_range_days", "path"):
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

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#btn-reset")
    def _reset(self) -> None:
        self.action_reset()
