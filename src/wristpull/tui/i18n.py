"""gettext wrapper: English source strings, a German catalog, switchable at runtime.

Call ``_()`` at compose/render time, never at import time, so a language switch
followed by a recompose picks up the new catalog.
"""

from __future__ import annotations

import gettext
import os
from collections.abc import Mapping
from pathlib import Path

from ..config import UI_LANGUAGES

DOMAIN = "wristpull"
LOCALE_DIR = Path(__file__).parent / "locale"
DEFAULT_LANGUAGE = "en"
_ENV_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")

_state: dict[str, object] = {"language": DEFAULT_LANGUAGE, "translations": gettext.NullTranslations()}


def normalize_language(value: str) -> str | None:
    """'de_DE.UTF-8' -> 'de'; returns None for unsupported or empty values."""
    code = value.strip().lower().replace("-", "_").split(".")[0].split("_")[0]
    return code if code in UI_LANGUAGES else None


def resolve_language(configured: str | None, env: Mapping[str, str] | None = None) -> str:
    """Config value first, then LANGUAGE/LC_ALL/LC_MESSAGES/LANG, then English."""
    env = os.environ if env is None else env
    candidates: list[str] = [configured] if configured else []
    for var in _ENV_VARS:
        value = env.get(var)
        if value:
            candidates.extend(value.split(":"))
    for candidate in candidates:
        code = normalize_language(candidate)
        if code:
            return code
    return DEFAULT_LANGUAGE


def set_language(language: str) -> str:
    """Activate a catalog; unknown codes fall back to English. Returns the active code."""
    code = normalize_language(language) or DEFAULT_LANGUAGE
    if code == DEFAULT_LANGUAGE:
        translations: gettext.NullTranslations = gettext.NullTranslations()
    else:
        translations = gettext.translation(DOMAIN, localedir=str(LOCALE_DIR), languages=[code], fallback=True)
    _state["language"] = code
    _state["translations"] = translations
    return code


def current_language() -> str:
    return str(_state["language"])


def next_language() -> str:
    """The language after the current one in UI_LANGUAGES (wraps around)."""
    index = UI_LANGUAGES.index(current_language())
    return UI_LANGUAGES[(index + 1) % len(UI_LANGUAGES)]


def _(message: str) -> str:
    return _state["translations"].gettext(message)  # type: ignore[attr-defined]


def ngettext(singular: str, plural: str, n: int) -> str:
    return _state["translations"].ngettext(singular, plural, n)  # type: ignore[attr-defined]
