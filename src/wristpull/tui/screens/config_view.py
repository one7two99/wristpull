"""Screen 8 (read-only for now): the active configuration."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll

from ...config import default_config_path
from ..i18n import _, current_language
from ..widgets import Panel
from .base import WristpullScreen


class ConfigScreen(WristpullScreen):
    NAME = "config"
    BINDINGS = [Binding("escape", "app.goto('home')", "Back", show=False)]

    def screen_title(self) -> str:
        return _("Settings")

    def keys(self) -> list[tuple[str, str]]:
        return [("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with VerticalScroll(id="config"):
            yield Panel(_("Configuration file"), id="config-path")
            yield Panel(_("Active values (editing comes in the next wave)"), id="config-text")

    def refresh_data(self) -> None:
        config = self.app.config
        path = config.path or default_config_path()
        state = _("exists") if config.path else _("missing, showing defaults")
        self.query_one("#config-path", Panel).set_lines([f"{path}  ({state})", _("UI language now: {lang}").format(lang=current_language())])
        self.query_one("#config-text", Panel).set_lines(config.render().rstrip().splitlines())
