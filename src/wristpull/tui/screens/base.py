"""Common behaviour of all wristpull screens."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header

from ..i18n import _
from ..widgets import KeyBar

APP_KEYS: list[tuple[str, str]] = []  # filled at render time by keys()


class WristpullScreen(Screen):
    """Header + content + translated key bar; subclasses implement body() and keys()."""

    NAME = "home"

    def screen_title(self) -> str:
        return ""

    def keys(self) -> list[tuple[str, str]]:
        return []

    def body(self) -> ComposeResult:
        yield from ()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield from self.body()
        yield KeyBar(self.keys() + [("/", _("palette")), ("?", _("help")), ("F12", _("language")), ("q", _("quit"))], id="keybar")

    def on_mount(self) -> None:
        self.app.sub_title = self.screen_title()
        self.refresh_data()

    def refresh_data(self) -> None:  # noqa: B027 - optional hook
        """Reload data from the database; called on mount and after a job finished."""

    def job_line(self, text: str) -> None:  # noqa: B027 - optional hook
        """A log line from the running job."""

    def job_finished(self, record) -> None:
        self.refresh_data()
