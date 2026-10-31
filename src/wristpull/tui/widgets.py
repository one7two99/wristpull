"""Small reusable widgets: key bar, bordered panel, KPI tile, job log."""

from __future__ import annotations

from textual.widgets import RichLog, Static

from .queries import Kpi


class KeyBar(Static):
    """Footer replacement whose labels are translated at render time."""

    DEFAULT_CSS = """
    KeyBar { dock: bottom; height: 1; background: $primary-background; color: $text; padding: 0 1; }
    """

    def __init__(self, keys: list[tuple[str, str]], **kwargs) -> None:
        super().__init__(markup=False, **kwargs)
        self.set_keys(keys)

    def set_keys(self, keys: list[tuple[str, str]]) -> None:
        self.update("  ".join(f"{key} {label}" for key, label in keys))


class Panel(Static):
    """A bordered text block with a title."""

    DEFAULT_CSS = """
    Panel { border: round $primary; padding: 0 1; height: auto; }
    """

    def __init__(self, title: str, text: str = "", **kwargs) -> None:
        super().__init__(text, markup=False, **kwargs)
        self.border_title = title

    def set_lines(self, lines: list[str]) -> None:
        self.update("\n".join(lines) if lines else "–")


class KpiTile(Static):
    DEFAULT_CSS = """
    KpiTile { width: 1fr; height: auto; padding: 0 1; }
    """

    def __init__(self, kpi: Kpi, **kwargs) -> None:
        super().__init__(markup=False, **kwargs)
        self.show(kpi)

    def show(self, kpi: Kpi) -> None:
        self.update(f"{kpi.label}\n{kpi.value}\n{kpi.spark}\n{kpi.delta}")


class JobLog(RichLog):
    DEFAULT_CSS = """
    JobLog { border: round $primary; height: 1fr; padding: 0 1; }
    """

    def __init__(self, lines: list[str] | None = None, **kwargs) -> None:
        super().__init__(markup=False, highlight=False, wrap=True, **kwargs)
        self._initial = list(lines or [])

    def on_mount(self) -> None:
        for line in self._initial:
            self.write(line)

    def append(self, text: str) -> None:
        self.write(text)
