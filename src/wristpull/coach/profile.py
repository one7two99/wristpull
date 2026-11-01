"""The goal profile: an editable Markdown prompt under <data_dir>/coach/profile.md."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from .prompts import DEFAULT_PROFILE, unfilled_placeholders
from .wizard import QUESTIONS, Question, answers_to_toml, generate_goal_prompt, merge_profile


def coach_dir(config: Config) -> Path:
    return config.data_dir / "coach"


def profile_path(config: Config) -> Path:
    return coach_dir(config) / "profile.md"


def ensure_profile(config: Config, lang: str) -> tuple[Path, bool]:
    """Create the default profile when none exists. Returns (path, created)."""
    path = profile_path(config)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_PROFILE.get(lang, DEFAULT_PROFILE["en"]), "utf-8")
    return path, True


def load_profile(config: Config, lang: str) -> tuple[str, str, list[str]]:
    """Returns (markdown, sha256, unfilled placeholders); creates the default file on first use."""
    path, _created = ensure_profile(config, lang)
    text = path.read_text("utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest(), unfilled_placeholders(text)


def answers_path(config: Config) -> Path:
    return coach_dir(config) / "profile.toml"


def load_answers(config: Config) -> dict[str, Any]:
    path = answers_path(config)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return dict(tomllib.load(fh))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def apply_wizard(config: Config, answers: dict[str, Any], lang: str) -> Path:
    """Write the generated block into profile.md (keeping the user's own text), back up the old file, store the answers."""
    path = profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text("utf-8") if path.exists() else None
    if existing and existing.strip() and existing.strip() not in (DEFAULT_PROFILE["en"].strip(), DEFAULT_PROFILE["de"].strip()):
        backup = path.with_name(f"profile.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md")
        backup.write_text(existing, "utf-8")
    elif existing and existing.strip() in (DEFAULT_PROFILE["en"].strip(), DEFAULT_PROFILE["de"].strip()):
        existing = None  # the untouched template carries nothing worth keeping
    path.write_text(merge_profile(existing, generate_goal_prompt(answers, lang), lang), "utf-8")
    answers_path(config).write_text(answers_to_toml(answers), "utf-8")
    return path


Asker = Callable[[Question, Any], Any]  # (question, previous answer) -> answer (str or list[str])


def run_wizard(config: Config, lang: str, ask: Asker) -> dict[str, Any]:
    """Ask every question through ``ask`` (CLI prompt or TUI form), prefilled with earlier answers."""
    previous = load_answers(config)
    answers: dict[str, Any] = {}
    for question in QUESTIONS:
        answer = ask(question, previous.get(question.key))
        if answer in (None, "", []):
            continue
        answers[question.key] = answer
    return answers


def cli_asker(lang: str, input_fn: Callable[[str], str] | None = None, print_fn: Callable[[str], None] = print) -> Asker:
    """Plain-text prompts for the terminal: numbered options (several comma-separated for multi), Enter keeps the previous answer."""
    lang = lang if lang in ("en", "de") else "en"
    if input_fn is None:
        import builtins

        input_fn = builtins.input  # resolved at call time so tests can patch it

    def ask(question: Question, previous: Any) -> Any:
        print_fn("")
        print_fn(question.text[lang])
        if question.options:
            for i, (value, label) in enumerate(question.options, 1):
                print_fn(f"  {i}) {label[lang]}")
            hint = ("numbers, comma-separated" if question.multi else "number") if lang == "en" else ("Nummern, kommagetrennt" if question.multi else "Nummer")
            shown = ",".join(str(i) for i, (v, _l) in enumerate(question.options, 1) if (v in previous if isinstance(previous, list) else v == previous)) if previous else ""
            raw = input_fn(f"  [{hint}] {('[' + shown + '] ') if shown else ''}> ").strip()
            if not raw:
                return previous
            chosen: list[str] = []
            for part in raw.replace(";", ",").split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(question.options):
                    chosen.append(question.options[int(part) - 1][0])
            if not chosen:
                return previous
            return chosen if question.multi else chosen[0]
        if question.hint:
            print_fn(f"  ({question.hint[lang]})")
        raw = input_fn(f"  {('[' + str(previous) + '] ') if previous else ''}> ").strip()
        return raw if raw else previous

    return ask
