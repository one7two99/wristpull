"""Per-request translations on the TUI's catalog (domain ``wristpull``).

The TUI switches one process-global language; a web server serves several browsers at once, so
each request gets its own ``gettext`` translation object instead.
"""

from __future__ import annotations

import gettext
from functools import lru_cache

from ..tui.i18n import DEFAULT_LANGUAGE, DOMAIN, LOCALE_DIR, normalize_language


@lru_cache(maxsize=None)
def translations(code: str) -> gettext.NullTranslations:
    if code == DEFAULT_LANGUAGE:
        return gettext.NullTranslations()
    return gettext.translation(DOMAIN, localedir=str(LOCALE_DIR), languages=[code], fallback=True)


def pick_language(configured: str | None, accept_language: str | None) -> str:
    """``[ui] language`` from the config first, then the browser's Accept-Language, then English."""
    if configured:
        code = normalize_language(configured)
        if code:
            return code
    weighted: list[tuple[float, str]] = []
    for part in (accept_language or "").split(","):
        tag, _sep, params = part.strip().partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                quality = 0.0
        if tag:
            weighted.append((quality, tag))
    for _quality, tag in sorted(weighted, key=lambda t: -t[0]):
        code = normalize_language(tag)
        if code:
            return code
    return DEFAULT_LANGUAGE
