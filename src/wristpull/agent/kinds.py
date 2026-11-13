"""The fixed list of jobs the agent runs, with typed parameters.

The browser never sends command lines or paths: it names a kind from this table and the agent
validates every parameter against the field list before the job is stored. Titles are the msgids
the TUI already uses, so both shells show the same words.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SERIAL_RE = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
NAME_RE = re.compile(r"^[^/\\\0]{1,120}$")  # workout file names; store.workout_file does the real check at run time


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # bool | int | str | names
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class KindSpec:
    key: str
    title: str  # msgid
    fields: tuple[Field, ...]
    stoppable: bool = False  # the library call polls ``stop`` between files
    device: str = "watch"  # watch | phone | strap, for the dashboard's hints

    def cli(self, params: Mapping[str, Any]) -> str:
        return CLI[self.key](params)


def _cli_import(p: Mapping[str, Any]) -> str:
    return "wristpull import garmin" + (" --dry-run" if p.get("dry_run") else "")


def _cli_phone(p: Mapping[str, Any]) -> str:
    cli = "wristpull pull phone"
    if p.get("serial"):
        cli += f" --serial {p['serial']}"
    if not p.get("trigger", True):
        cli += " --no-trigger"
    elif not p.get("sync", True):
        cli += " --no-sync"
    if p.get("wait", 60) != 60:
        cli += f" --wait {p['wait']}"
    if p.get("dry_run"):
        cli += " --dry-run"
    return cli


def _cli_strap(p: Mapping[str, Any]) -> str:
    cli = "wristpull pull strap"
    if p.get("since_days") is not None:
        cli += f" --since-days {p['since_days']}"
    if p.get("dry_run"):
        cli += " --dry-run"
    return cli


def _cli_push(p: Mapping[str, Any]) -> str:
    cli = "wristpull workout push " + " ".join(shlex.quote(n) for n in p.get("names", []))
    return cli + (" --schedule" if p.get("schedule") else "") + (" --force" if p.get("force") else "") + (" --dry-run" if p.get("dry_run") else "")


def _cli_push_upcoming(p: Mapping[str, Any]) -> str:
    return "wristpull workout push --upcoming" + (" --force" if p.get("force") else "") + (" --dry-run" if p.get("dry_run") else "")


def _cli_remove(p: Mapping[str, Any]) -> str:
    return "wristpull workout remove " + " ".join(shlex.quote(n) for n in p.get("keys", [])) + " --yes"


CLI = {
    "import_garmin": _cli_import,
    "pull_phone": _cli_phone,
    "pull_strap": _cli_strap,
    "workout_push": _cli_push,
    "workout_push_upcoming": _cli_push_upcoming,
    "workout_verify": lambda p: "wristpull workout verify",
    "watch_list": lambda p: "wristpull workout watch",
    "watch_remove": _cli_remove,
}

KINDS: dict[str, KindSpec] = {
    spec.key: spec
    for spec in (
        KindSpec("import_garmin", "Import Garmin", (Field("dry_run", "bool", False), Field("auto", "bool", False)), stoppable=True),
        KindSpec(
            "pull_phone",
            "Pull phone",
            (
                Field("serial", "str", ""),
                Field("trigger", "bool", True),
                Field("sync", "bool", True),
                Field("wait", "int", 60, minimum=0, maximum=900),
                Field("dry_run", "bool", False),
            ),
            stoppable=True,
            device="phone",
        ),
        KindSpec("pull_strap", "Pull strap", (Field("dry_run", "bool", False), Field("since_days", "int", None, minimum=1, maximum=3650)), stoppable=True, device="strap"),
        KindSpec("workout_push", "Push workouts", (Field("names", "names"), Field("dry_run", "bool", False), Field("schedule", "bool", False), Field("force", "bool", False))),
        KindSpec("workout_push_upcoming", "Push workouts", (Field("dry_run", "bool", False), Field("force", "bool", False))),
        KindSpec("workout_verify", "Verify workouts", ()),
        KindSpec("watch_list", "Workouts on the watch", ()),
        KindSpec("watch_remove", "Remove workouts from the watch", (Field("keys", "names"),)),
    )
}

TRUE_WORDS = ("1", "true", "on", "yes")


def validate(kind: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of ``kind`` with every field typed and defaulted; raises ``ValueError`` on anything else."""
    spec = KINDS.get(kind)
    if spec is None:
        raise ValueError(f"unknown job kind: {kind}")
    allowed = {f.name for f in spec.fields}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unknown parameter(s) for {kind}: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for f in spec.fields:
        raw = params.get(f.name, f.default)
        if f.type == "bool":
            value = raw.strip().lower() in TRUE_WORDS if isinstance(raw, str) else bool(raw)
        elif f.type == "int":
            if raw is None or raw == "":
                value = f.default
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{f.name} must be a whole number") from exc
                if f.minimum is not None and value < f.minimum or f.maximum is not None and value > f.maximum:
                    raise ValueError(f"{f.name} must be between {f.minimum} and {f.maximum}")
        elif f.type == "str":
            value = str(raw or "").strip()
            if value and not SERIAL_RE.match(value):
                raise ValueError(f"{f.name} contains characters that are not allowed")
        elif f.type == "names":
            if isinstance(raw, str):
                raw = [raw]
            names = [str(n).strip() for n in (raw or []) if str(n).strip()]
            if not names:
                raise ValueError(f"{f.name}: at least one name is needed")
            for name in names:
                if not NAME_RE.match(name) or name in (".", ".."):
                    raise ValueError(f"{f.name}: {name!r} is not a file name")
            value = names
        else:  # pragma: no cover - the table above only uses the four types
            raise ValueError(f"bad field type {f.type}")
        out[f.name] = value
    return out
