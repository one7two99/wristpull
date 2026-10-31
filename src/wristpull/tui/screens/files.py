"""Screen 6: browse the data directory (raw archive, exports, reports) and inspect FIT files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DirectoryTree

from ..format import fmt_bytes, fmt_datetime, fmt_int
from ..i18n import _
from ..queries import inspect_fit, raw_file_info
from ..widgets import Panel
from .base import WristpullScreen


class FilesScreen(WristpullScreen):
    NAME = "files"
    BINDINGS = [
        Binding("o", "open_selected", "Open", show=False),
        Binding("y", "copy_path", "Copy path", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected: Path | None = None

    def screen_title(self) -> str:
        return _("Files")

    def keys(self) -> list[tuple[str, str]]:
        return [("Enter", _("inspect")), ("o", _("open")), ("y", _("copy path")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Horizontal(id="files"):
            tree = DirectoryTree(self.app.config.data_dir, id="tree")
            tree.border_title = str(self.app.config.data_dir)
            yield tree
            with Vertical(id="file-side"):
                yield Panel(_("File"), id="file-info")
                yield Panel(_("Messages (inspect)"), id="file-inspect")

    def refresh_data(self) -> None:
        if self.selected is None:
            self.query_one("#file-info", Panel).set_lines([_("Select a file in the tree. Enter on a .fit file runs inspect.")])
            self.query_one("#file-inspect", Panel).set_lines([])

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.show_file(Path(event.path))

    def show_file(self, path: Path) -> None:
        self.selected = path
        info = self.query_one("#file-info", Panel)
        info.border_title = path.name
        lines = []
        try:
            stat = path.stat()
            lines.append(_("Size {size}").format(size=fmt_bytes(stat.st_size)))
            lines.append(_("Modified {when}").format(when=fmt_datetime(datetime.fromtimestamp(stat.st_mtime, UTC).isoformat())))
        except OSError as exc:
            lines.append(str(exc))
        row = raw_file_info(self.app.conn, self.app.config, path)
        if row:
            lines.append(f"{_('Source')} {row['source']}   {_('Kind')} {row['kind']}")
            lines.append(f"SHA-256 {row['sha256'][:16]}…")
            lines.append(_("Imported {when}").format(when=fmt_datetime(row["imported_at_utc"])))
            status = row["parse_status"]
            lines.append(_("Parse status {status}").format(status=status) + (f": {row['parse_error']}" if row.get("parse_error") else ""))
            if row.get("device_product"):
                lines.append(_("Device {product} (serial {serial})").format(product=row["device_product"], serial=row.get("device_serial") or "–"))
            if row.get("origin_path"):
                lines.append(_("Origin {path}").format(path=row["origin_path"]))
        lines.append(str(path))
        info.set_lines(lines)
        inspect_panel = self.query_one("#file-inspect", Panel)
        if path.suffix.lower() == ".fit":
            self._inspect(path, inspect_panel)
        elif path.suffix.lower() in {".md", ".txt", ".csv", ".toml"}:
            try:
                text = path.read_text("utf-8", errors="replace").splitlines()[:40]
            except OSError as exc:
                text = [str(exc)]
            inspect_panel.border_title = _("Preview")
            inspect_panel.set_lines(text)
        else:
            inspect_panel.border_title = _("Messages (inspect)")
            inspect_panel.set_lines([])

    def _inspect(self, path: Path, panel: Panel) -> None:
        try:
            header, summary = inspect_fit(path)
        except Exception as exc:  # decoding errors of corrupt files are shown, not raised
            panel.set_lines([_("Could not decode: {error}").format(error=exc)])
            return
        panel.border_title = _("Messages (inspect)") + f"  {header['type']} · {header['product'] or ''}"
        lines = [
            _("{n} messages in {t} types; created {created}").format(n=fmt_int(header["messages"]), t=len(summary), created=header["created"] or "–"),
        ]
        if header["warnings"]:
            lines.append(_("{n} decoder warnings").format(n=header["warnings"]))
        for name, entry in summary.items():
            fields = ", ".join(entry["fields"].keys())
            lines.append(f"{name} (#{entry['num']}): {fmt_int(entry['count'])}  {fields[:70]}")
        panel.set_lines(lines)

    def action_open_selected(self) -> None:
        if self.selected is None:
            self.app.notify(_("Nothing selected"), severity="warning")
            return
        self.app.deps.opened.append(self.selected)
        self.app.deps.open_path(self.selected)
        self.app.notify(_("Opened {path}").format(path=self.selected.name))

    def action_copy_path(self) -> None:
        if self.selected is None:
            self.app.notify(_("Nothing selected"), severity="warning")
            return
        self.app.copy_to_clipboard(str(self.selected))
        self.app.notify(_("Copied path"))
