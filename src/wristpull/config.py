"""Configuration: ~/.config/wristpull/config.toml with sensible defaults."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .runtime import CONFIG_FILE_ENV, DATA_DIR_ENV
from .timeutil import detect_local_zone

DEFAULT_GARMIN_FOLDERS = ["Activity", "Monitor", "Sleep", "Metrics", "HRVStatus"]
GARMIN_ACCESS = ("auto", "gvfs", "mtp")

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
# How the watch is reached: "auto" (MTP tools inside the Docker container, a gvfs/USB mount otherwise),
# "gvfs" (a mounted GARMIN folder) or "mtp" (libmtp command-line tools, no mount).
access = "{garmin_access}"

[phone]
# ADB serial of the phone running Gadgetbridge.
serial = "{phone_serial}"
# Gadgetbridge export directory on the phone.
export_dir = "/sdcard/Gadgetbridge"

[strap]
# Bluetooth address of the Zepp OS band (Amazfit Helio Strap) pulled directly over BLE; empty: not used.
# `wristpull strap scan` lists the bands in range, `wristpull strap pair` stores the auth key.
address = "{strap_address}"
# Bluetooth adapter (empty: the default one).
adapter = "{strap_adapter}"
# The band's 16-byte auth key lives in this file (never in the config): <data_dir>/strap/authkey by default.
authkey_file = "{strap_authkey_file}"
# How far back the first pull asks the band for data.
since_days = {strap_since_days}
# Leave the data on the band after fetching (false: the band may delete what it sent).
keep_on_band = {strap_keep_on_band}

[ui]
# Language of the terminal UI (wristpull tui): "de", "en" or "" for the system locale.
language = "{ui_language}"
# Textual theme name, e.g. textual-dark, textual-light, nord, gruvbox.
theme = "{ui_theme}"
# Default number of days shown in the data screens.
range_days = {ui_range_days}

[coach]
# LLM backend for the training coach: ollama | claude | openrouter
backend = "{coach_backend}"
model = "{coach_model}"
# Optional larger model for the weekly review (empty: same as model).
weekly_model = "{coach_weekly_model}"
# Environment variable holding the API key (never the key itself). Empty: ANTHROPIC_API_KEY / OPENROUTER_API_KEY.
api_key_env = "{coach_api_key_env}"
ollama_url = "{coach_ollama_url}"
# Ollama context window in tokens (the data package plus the answer must fit; memory grows with it).
num_ctx = {coach_num_ctx}
max_tokens = {coach_max_tokens}
timeout_s = {coach_timeout_s}
# Roughly how many tokens of data the coach may send (local models: 6000).
token_budget = {coach_token_budget}
# Claude only: low | medium | high
effort = "{coach_effort}"
# Answer language: de | en | "" for the UI language
language = "{coach_language}"
{coach_prices}
[workout]
# Shape of generated workouts (templates and drafts from the weekly plan): minutes and HR caps of the warm-up and cool-down.
warmup_min = {workout_warmup_min}
cooldown_min = {workout_cooldown_min}
warmup_hr_max = {workout_warmup_hr_max}
cooldown_hr_max = {workout_cooldown_hr_max}
# Heart-rate target of a generated main part: a zone (Z2), a bpm range (135-150) or percent of max HR (70-80 %).
default_target = "{workout_default_target}"
# Also write a schedule file so the watch shows the workout in its calendar (experimental).
schedule = {workout_schedule}
# Folder below GARMIN/ the FIT files are copied to: NewFiles (processed when the cable is unplugged) or Workouts.
folder = "{workout_folder}"
# Unmount (eject) the MTP mount after a push. Off by default: the fenix keeps MTP-written files without it.
eject = {workout_eject}
"""

UI_LANGUAGES = ("en", "de")
WORKOUT_FOLDERS = ("NewFiles", "Workouts")
COACH_BACKENDS = ("ollama", "claude", "openrouter")
COACH_EFFORTS = ("low", "medium", "high")
# USD per million input / output tokens; used for the cost estimate of a run (overridable in [coach.prices]).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}


@dataclass
class CoachConfig:
    backend: str = "ollama"
    model: str = "gemma3:4b"
    weekly_model: str | None = None
    api_key_env: str | None = None
    ollama_url: str = "http://localhost:11434"
    num_ctx: int = 8192  # Ollama context window; must hold the data package plus the answer
    max_tokens: int = 3000
    timeout_s: float = 600.0
    token_budget: int = 6000
    effort: str = "medium"
    language: str | None = None
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    def price_for(self, model: str) -> tuple[float, float] | None:
        return self.prices.get(model) or DEFAULT_PRICES.get(model)


@dataclass
class WorkoutConfig:
    warmup_min: int = 10
    cooldown_min: int = 5
    warmup_hr_max: int = 130
    cooldown_hr_max: int = 125
    default_target: str = "Z2"
    schedule: bool = False
    folder: str = "NewFiles"
    eject: bool = False

    def defaults(self):
        from .workouts.model import Defaults

        return Defaults(self.warmup_min, self.cooldown_min, self.warmup_hr_max, self.cooldown_hr_max, self.default_target)


def is_bluetooth_address(text: str) -> bool:
    parts = text.strip().split(":")
    return len(parts) == 6 and all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``WRISTPULL_CONFIG_FILE`` if set (the Docker ``cli`` service), else ``$XDG_CONFIG_HOME/wristpull/config.toml``."""
    env = os.environ if env is None else env
    explicit = env.get(CONFIG_FILE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "wristpull" / "config.toml"


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path.home() / "Health" / "wristpull")
    timezone: str = field(default_factory=detect_local_zone)
    garmin_folders: list[str] = field(default_factory=lambda: list(DEFAULT_GARMIN_FOLDERS))
    garmin_mount_hints: list[Path] = field(default_factory=list)
    garmin_access: str = "auto"  # auto | gvfs | mtp, see GARMIN_ACCESS
    phone_serial: str | None = None
    phone_export_dir: str = "/sdcard/Gadgetbridge"
    strap_address: str = ""
    strap_adapter: str = ""
    strap_authkey_file: str = ""  # empty: <data_dir>/strap/authkey
    strap_since_days: int = 30
    strap_keep_on_band: bool = True
    ui_language: str | None = None  # None: follow the system locale
    ui_theme: str = "textual-dark"
    ui_range_days: int = 28
    coach: CoachConfig = field(default_factory=CoachConfig)
    workout: WorkoutConfig = field(default_factory=WorkoutConfig)
    path: Path | None = None  # where this config was loaded from, if anywhere
    # [storage] data_dir as written in the file while WRISTPULL_DATA_DIR overrides it (the container
    # mounts the directory elsewhere); render() keeps the file's value so the host config stays valid.
    file_data_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wristpull.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def strap_authkey_path(self) -> Path:
        return Path(self.strap_authkey_file).expanduser() if self.strap_authkey_file else self.data_dir / "strap" / "authkey"

    def render(self) -> str:
        folders = "[" + ", ".join(f'"{f}"' for f in self.garmin_folders) + "]"
        hints = "[" + ", ".join(f'"{str(h).replace(str(Path.home()), "~", 1)}"' for h in self.garmin_mount_hints) + "]"
        return EXAMPLE_CONFIG.format(
            data_dir=str(self.file_data_dir or self.data_dir).replace(str(Path.home()), "~", 1),
            timezone=self.timezone,
            folders=folders,
            mount_hints=hints,
            garmin_access=self.garmin_access,
            phone_serial=self.phone_serial or "",
            strap_address=self.strap_address,
            strap_adapter=self.strap_adapter,
            strap_authkey_file=self.strap_authkey_file,
            strap_since_days=self.strap_since_days,
            strap_keep_on_band="true" if self.strap_keep_on_band else "false",
            ui_language=self.ui_language or "",
            ui_theme=self.ui_theme,
            ui_range_days=self.ui_range_days,
            coach_backend=self.coach.backend,
            coach_model=self.coach.model,
            coach_weekly_model=self.coach.weekly_model or "",
            coach_api_key_env=self.coach.api_key_env or "",
            coach_ollama_url=self.coach.ollama_url,
            coach_num_ctx=self.coach.num_ctx,
            coach_max_tokens=self.coach.max_tokens,
            coach_timeout_s=f"{self.coach.timeout_s:g}",
            coach_token_budget=self.coach.token_budget,
            coach_effort=self.coach.effort,
            coach_language=self.coach.language or "",
            coach_prices=(
                "\n[coach.prices]\n" + "".join(f'"{m}" = {{ input = {i:g}, output = {o:g} }}\n' for m, (i, o) in sorted(self.coach.prices.items()))
                if self.coach.prices
                else ""
            ),
            workout_warmup_min=self.workout.warmup_min,
            workout_cooldown_min=self.workout.cooldown_min,
            workout_warmup_hr_max=self.workout.warmup_hr_max,
            workout_cooldown_hr_max=self.workout.cooldown_hr_max,
            workout_default_target=self.workout.default_target,
            workout_schedule="true" if self.workout.schedule else "false",
            workout_folder=self.workout.folder,
            workout_eject="true" if self.workout.eject else "false",
        )


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value))


def load_config(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """Load the config file; a missing file yields defaults.

    ``WRISTPULL_DATA_DIR`` (set by the Docker ``cli`` service) overrides ``[storage] data_dir``;
    the file's own value is kept in ``file_data_dir`` so that writing the config back does not
    bake the container path into the host's file.
    """
    env = os.environ if env is None else env
    cfg_path = path or default_config_path(env)
    cfg = Config(path=cfg_path if cfg_path.exists() else None)
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    else:
        data = {}
    storage = data.get("storage", {})
    if "data_dir" in storage:
        cfg.data_dir = _expand(storage["data_dir"])
    if env.get(DATA_DIR_ENV, "").strip():
        cfg.file_data_dir = cfg.data_dir
        cfg.data_dir = Path(env[DATA_DIR_ENV].strip()).expanduser()
    if not cfg_path.exists():
        return cfg
    time_section = data.get("time", {})
    if time_section.get("timezone"):
        cfg.timezone = time_section["timezone"]
    garmin = data.get("garmin", {})
    if "folders" in garmin:
        cfg.garmin_folders = [str(f) for f in garmin["folders"]]
    if "mount_hints" in garmin:
        cfg.garmin_mount_hints = [_expand(str(p)) for p in garmin["mount_hints"]]
    if garmin.get("access"):
        cfg.garmin_access = str(garmin["access"]).strip().lower()
    phone = data.get("phone", {})
    if phone.get("serial"):
        cfg.phone_serial = str(phone["serial"])
    if phone.get("export_dir"):
        cfg.phone_export_dir = str(phone["export_dir"])
    strap = data.get("strap", {})
    cfg.strap_address = str(strap.get("address", "") or "").strip()
    cfg.strap_adapter = str(strap.get("adapter", "") or "").strip()
    cfg.strap_authkey_file = str(strap.get("authkey_file", "") or "").strip()
    if strap.get("since_days") is not None:
        cfg.strap_since_days = int(strap["since_days"])
    if strap.get("keep_on_band") is not None:
        cfg.strap_keep_on_band = bool(strap["keep_on_band"])
    ui = data.get("ui", {})
    if ui.get("language"):
        cfg.ui_language = str(ui["language"])
    if ui.get("theme"):
        cfg.ui_theme = str(ui["theme"])
    if "range_days" in ui:
        cfg.ui_range_days = max(1, int(ui["range_days"]))
    coach = data.get("coach", {})
    for key in ("backend", "model", "ollama_url", "effort"):
        if coach.get(key):
            setattr(cfg.coach, key, str(coach[key]))
    for key in ("weekly_model", "api_key_env", "language"):
        if coach.get(key):
            setattr(cfg.coach, key, str(coach[key]))
    for key in ("max_tokens", "token_budget", "num_ctx"):
        if key in coach:
            setattr(cfg.coach, key, int(coach[key]))
    if "timeout_s" in coach:
        cfg.coach.timeout_s = float(coach["timeout_s"])
    prices = coach.get("prices", {})
    if isinstance(prices, dict):
        for model, entry in prices.items():
            if isinstance(entry, dict) and "input" in entry and "output" in entry:
                cfg.coach.prices[str(model)] = (float(entry["input"]), float(entry["output"]))
    workout = data.get("workout", {})
    for key in ("warmup_min", "cooldown_min", "warmup_hr_max", "cooldown_hr_max"):
        if key in workout:
            setattr(cfg.workout, key, int(workout[key]))
    if "default_target" in workout:
        cfg.workout.default_target = str(workout["default_target"])
    if "schedule" in workout:
        cfg.workout.schedule = bool(workout["schedule"])
    if workout.get("folder"):
        cfg.workout.folder = str(workout["folder"])
    if "eject" in workout:
        cfg.workout.eject = bool(workout["eject"])
    return cfg


def config_file_present(path: Path) -> bool:
    """A config exists when the file has content: Docker bind-mounts the file, so an empty one is often
    created first (``touch``) just to have something to mount, and must count as missing."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    cfg_path = path or default_config_path()
    if config_file_present(cfg_path) and not overwrite:
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
    if cfg.garmin_access not in GARMIN_ACCESS:
        problems.append(f"garmin access must be one of {', '.join(GARMIN_ACCESS)}")
    if cfg.ui_language and cfg.ui_language not in UI_LANGUAGES:
        problems.append(f"ui language must be one of {', '.join(UI_LANGUAGES)} or empty")
    if cfg.ui_range_days < 1:
        problems.append("range_days must be at least 1")
    if not cfg.phone_export_dir.startswith("/"):
        problems.append("phone export_dir must be an absolute path on the phone")
    if cfg.strap_address and not is_bluetooth_address(cfg.strap_address):
        problems.append("strap address must look like AA:BB:CC:DD:EE:FF")
    if cfg.strap_since_days < 1:
        problems.append("strap since_days must be at least 1")
    coach = cfg.coach
    if coach.backend not in COACH_BACKENDS:
        problems.append(f"coach backend must be one of {', '.join(COACH_BACKENDS)}")
    if not coach.model.strip():
        problems.append("coach model must not be empty")
    if coach.max_tokens < 100:
        problems.append("coach max_tokens must be at least 100")
    if coach.token_budget < 1000:
        problems.append("coach token_budget must be at least 1000")
    if coach.num_ctx < 2048:
        problems.append("coach num_ctx must be at least 2048")
    if coach.timeout_s <= 0:
        problems.append("coach timeout_s must be positive")
    if coach.effort not in COACH_EFFORTS:
        problems.append(f"coach effort must be one of {', '.join(COACH_EFFORTS)}")
    if coach.language and coach.language not in UI_LANGUAGES:
        problems.append(f"coach language must be one of {', '.join(UI_LANGUAGES)} or empty")
    problems.extend(validate_workout(cfg.workout))
    return problems


def validate_workout(w: WorkoutConfig) -> list[str]:
    from .workouts.model import HR_BPM_MAX, HR_BPM_MIN, parse_target

    problems: list[str] = []
    if w.warmup_min < 0 or w.cooldown_min < 0:
        problems.append("workout warmup_min and cooldown_min must not be negative")
    for label, value in (("warmup_hr_max", w.warmup_hr_max), ("cooldown_hr_max", w.cooldown_hr_max)):
        if not HR_BPM_MIN <= value <= HR_BPM_MAX:
            problems.append(f"workout {label} must be between {HR_BPM_MIN} and {HR_BPM_MAX} bpm")
    if w.default_target:
        _hr, error = parse_target(w.default_target)
        if error:
            problems.append(f"workout default_target: {error}")
    if w.folder not in WORKOUT_FOLDERS:
        problems.append(f"workout folder must be one of {', '.join(WORKOUT_FOLDERS)}")
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
