"""Help overlay with the key bindings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

from ..i18n import _


def help_lines() -> list[str]:
    return [
        _("Everywhere") + ":  1-9 0 " + _("screens") + "   / " + _("command palette") + "   ? " + _("help") + "   F12 " + _("language") + "   q " + _("quit"),
        "   " + _("Esc back") + "   Tab " + _("focus") + "   ↑↓ ←→ " + _("move") + "   Enter " + _("select"),
        "",
        "1 " + _("Overview") + ":  i " + _("import Garmin") + "   p " + _("pull phone") + "   s " + _("refresh") + "   e " + _("export") + "   r " + _("weekly report"),
        "2 " + _("Devices & Sync") + ":  i / p " + _("start") + "   m " + _("mount") + "   x " + _("stop job") + "   l " + _("clear log") + "   c " + _("copy CLI"),
        "3 " + _("Days") + ", 4 " + _("Activities") + ", 5 " + _("Sleep") + ":  ←→ " + _("shift period") + "   w " + _("this week") + "   m " + _("this month") + "   d " + _("default range"),
        "4 " + _("Activities") + ":  f " + _("filter") + "   o " + _("open file") + "      5 " + _("Sleep") + ":  g " + _("source"),
        "6 " + _("Files") + ":  Enter " + _("inspect") + "   o " + _("open") + "   y " + _("copy path"),
        "7 " + _("Export & Report") + ":  e " + _("generate") + "   o " + _("open folder") + "   y " + _("copy path") + "   c " + _("copy CLI"),
        "8 " + _("Settings") + ":  Ctrl+S " + _("save") + "   Ctrl+R " + _("reset") + "   Ctrl+T " + _("check coach backend"),
        "9 " + _("Coach") + ":  d " + _("daily") + "   w " + _("weekly") + "   Enter " + _("ask") + "   n " + _("journal note") + "   p " + _("dry run") + "   + − 0 " + _("rate") + "   g " + _("goal wizard") + "   e " + _("edit profile") + "   t " + _("draft workouts"),
        "0 " + _("Workouts") + ":  n " + _("new") + "   t " + _("template") + "   e " + _("edit") + "   c " + _("drafts from plan") + "   p " + _("push") + "   P " + _("push upcoming") + "   f " + _("force push") + "   v " + _("verify") + "   Del " + _("delete") + "   ←→ " + _("shift period"),
        "   w " + _("files / watch") + ":  " + _("on the watch") + "  r " + _("read watch") + "   m " + _("mount") + "   Del " + _("remove from watch"),
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
