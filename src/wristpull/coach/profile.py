"""The goal profile: an editable Markdown prompt under <data_dir>/coach/profile.md."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Config
from .prompts import DEFAULT_PROFILE, unfilled_placeholders


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
