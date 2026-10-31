"""Help overlay with the key bindings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

from ..i18n import _


def help_lines() -> list[str]:
    return [
        _("Everywhere") + ":  1-8 " + _("screens") + "   / " + _("command palette") + "   ? " + _("help") + "   F12 " + _("language") + "   q " + _("quit"),
        "   " + _("Esc back") + "   Tab " + _("focus") + "   ↑↓ ←→ " + _("move") + "   Enter " + _("select"),
        "",
        "1 " + _("Overview") + ":  i " + _("import Garmin") + "   p " + _("pull phone") + "   s " + _("refresh") + "   e " + _("export") + "   r " + _("weekly report"),
        "2 " + _("Devices & Sync") + ":  i / p " + _("start") + "   l " + _("clear log") + "   c " + _("copy CLI"),
        "3 " + _("Days") + ":  ←→ " + _("shift period") + "   w " + _("this week") + "   m " + _("this month") + "   d " + _("default range"),
        "6 " + _("Files") + ":  Enter " + _("inspect") + "   o " + _("open") + "   y " + _("copy path"),
        "8 " + _("Settings") + ":  " + _("read-only in this version"),
        "",
        _("4 Activities, 5 Sleep and 7 Export & Report screens come in the next wave; e and r already run the export/report."),
    ]


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "Close", show=False)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Static { width: 96; max-width: 100%; height: auto; border: round $primary; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        text = Static("\n".join(help_lines()), markup=False)
        text.border_title = _("Help")
        yield text
