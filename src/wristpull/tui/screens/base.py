"""Common behaviour of all wristpull screens."""

from __future__ import annotations

from datetime import date, timedelta

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header

from ...aggregate import month_bounds, week_bounds
from ..format import fmt_date
from ..jobs import last_days

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


class PeriodMixin:
    """Shared period handling for the data screens: ←/→ shift, w week, m month, d default range."""

    period_start: date | None = None
    period_end: date | None = None

    def ensure_period(self) -> tuple[date, date]:
        if self.period_start is None or self.period_end is None:
            self.period_start, self.period_end = last_days(self.app.deps.today(), self.app.config.ui_range_days)  # type: ignore[attr-defined]
        return self.period_start, self.period_end

    def period_label(self) -> str:
        start, end = self.ensure_period()
        return f"{fmt_date(start, year=True)} – {fmt_date(end, year=True)}"

    def action_shift(self, direction: int) -> None:
        start, end = self.ensure_period()
        span = (end - start).days + 1
        self.period_start, self.period_end = start + timedelta(days=direction * span), end + timedelta(days=direction * span)
        self.refresh_data()  # type: ignore[attr-defined]

    def action_preset(self, which: str) -> None:
        today = self.app.deps.today()  # type: ignore[attr-defined]
        if which == "week":
            self.period_start, self.period_end = week_bounds(today)
        elif which == "month":
            self.period_start, self.period_end = month_bounds(today)
        else:
            self.period_start, self.period_end = last_days(today, self.app.config.ui_range_days)  # type: ignore[attr-defined]
        self.refresh_data()  # type: ignore[attr-defined]

    @staticmethod
    def period_keys() -> list[tuple[str, str]]:
        return [("←→", _("shift period")), ("w", _("this week")), ("m", _("this month")), ("d", _("default range"))]
