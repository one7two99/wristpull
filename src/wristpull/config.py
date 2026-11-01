"""Configuration: ~/.config/wristpull/config.toml with sensible defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import detect_local_zone

DEFAULT_GARMIN_FOLDERS = ["Activity", "Monitor", "Sleep", "Metrics", "HRVStatus"]

EXAMPLE_CONFIG = """\
# wristpull configuration
# All paths may use ~ for the home directory.

[storage]
# Raw files land in <data_dir>/raw/<source>/<year>/..., the database in <data_dir>/wristpull.sqlite
data_dir = "{data_dir}"

[time]
# IANA zone used to derive local dates from the UTC timestamps in the device files.
timezone = "{timezone}"

[garmin]
# Folders below GARMIN/ on the watch that are imported.
folders = {folders}
# Additional directories to search for a mounted watch (a directory that contains GARMIN/).
mount_hints = {mount_hints}

[phone]
# ADB serial of the phone running Gadgetbridge.
serial = "{phone_serial}"
# Gadgetbridge export directory on the phone.
export_dir = "/sdcard/Gadgetbridge"

[ui]
# Language of the terminal UI (wristpull tui): "de", "en" or "" for the system locale.
language = "{ui_language}"
# Textual theme name, e.g. textual-dark, textual-light, nord, gruvbox.
theme = "{ui_theme}"
# Default number of days shown in the data screens.
range_days = {ui_range_days}
"""

UI_LANGUAGES = ("en", "de")


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "wristpull" / "config.toml"


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path.home() / "Health" / "wristpull")
    timezone: str = field(default_factory=detect_local_zone)
    garmin_folders: list[str] = field(default_factory=lambda: list(DEFAULT_GARMIN_FOLDERS))
    garmin_mount_hints: list[Path] = field(default_factory=list)
    phone_serial: str | None = None
    phone_export_dir: str = "/sdcard/Gadgetbridge"
    ui_language: str | None = None  # None: follow the system locale
    ui_theme: str = "textual-dark"
    ui_range_days: int = 28
    path: Path | None = None  # where this config was loaded from, if anywhere

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wristpull.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    def render(self) -> str:
        folders = "[" + ", ".join(f'"{f}"' for f in self.garmin_folders) + "]"
        hints = "[" + ", ".join(f'"{str(h).replace(str(Path.home()), "~", 1)}"' for h in self.garmin_mount_hints) + "]"
        return EXAMPLE_CONFIG.format(
            data_dir=str(self.data_dir).replace(str(Path.home()), "~", 1),
            timezone=self.timezone,
            folders=folders,
            mount_hints=hints,
            phone_serial=self.phone_serial or "",
            ui_language=self.ui_language or "",
            ui_theme=self.ui_theme,
            ui_range_days=self.ui_range_days,
        )


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value))


def load_config(path: Path | None = None) -> Config:
    """Load the config file; a missing file yields defaults."""
    cfg_path = path or default_config_path()
    cfg = Config(path=cfg_path if cfg_path.exists() else None)
    if not cfg_path.exists():
        return cfg
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)
    storage = data.get("storage", {})
    if "data_dir" in storage:
        cfg.data_dir = _expand(storage["data_dir"])
    time_section = data.get("time", {})
    if time_section.get("timezone"):
        cfg.timezone = time_section["timezone"]
    garmin = data.get("garmin", {})
    if "folders" in garmin:
        cfg.garmin_folders = [str(f) for f in garmin["folders"]]
    if "mount_hints" in garmin:
        cfg.garmin_mount_hints = [_expand(str(p)) for p in garmin["mount_hints"]]
    phone = data.get("phone", {})
    if phone.get("serial"):
        cfg.phone_serial = str(phone["serial"])
    if phone.get("export_dir"):
        cfg.phone_export_dir = str(phone["export_dir"])
    ui = data.get("ui", {})
    if ui.get("language"):
        cfg.ui_language = str(ui["language"])
    if ui.get("theme"):
        cfg.ui_theme = str(ui["theme"])
    if "range_days" in ui:
        cfg.ui_range_days = max(1, int(ui["range_days"]))
    return cfg


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    cfg_path = path or default_config_path()
    if cfg_path.exists() and not overwrite:
        raise FileExistsError(cfg_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(Config().render(), "utf-8")
    return cfg_path


def validate_config(cfg: Config) -> list[str]:
    """Problems a settings editor should refuse to save (empty list = fine)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    problems: list[str] = []
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        problems.append(f"unknown timezone: {cfg.timezone!r}")
    if not str(cfg.data_dir).strip():
        problems.append("data_dir must not be empty")
    if not cfg.garmin_folders:
        problems.append("at least one Garmin folder is needed")
    if cfg.ui_language and cfg.ui_language not in UI_LANGUAGES:
        problems.append(f"ui language must be one of {', '.join(UI_LANGUAGES)} or empty")
    if cfg.ui_range_days < 1:
        problems.append("range_days must be at least 1")
    if not cfg.phone_export_dir.startswith("/"):
        problems.append("phone export_dir must be an absolute path on the phone")
    return problems


def write_config(cfg: Config, path: Path | None = None) -> Path:
    """Write the config file from the dataclass (comments of a hand-edited file are not preserved)."""
    problems = validate_config(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    cfg_path = path or cfg.path or default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(cfg.render(), "utf-8")
    cfg.path = cfg_path
    return cfg_path
