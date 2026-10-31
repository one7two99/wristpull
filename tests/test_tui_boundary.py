"""The TUI must not contain SQL or FIT parsing: it only calls library functions."""

import re
from pathlib import Path

TUI_DIR = Path(__file__).resolve().parents[1] / "src" / "wristpull" / "tui"
FORBIDDEN = re.compile(r"\bSELECT\b|\.execute\(|^import fitdecode|^from fitdecode", re.MULTILINE)


def test_tui_has_no_sql_or_fit_parsing():
    offenders = []
    for path in TUI_DIR.rglob("*.py"):
        text = path.read_text("utf-8")
        for match in FORBIDDEN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(TUI_DIR)}:{line}: {match.group(0)}")
    assert not offenders, offenders


def test_tui_strings_are_translated_at_render_time():
    """``_()`` at module level would freeze the language at import; only allow it inside functions."""
    offenders = []
    for path in TUI_DIR.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if re.match(r"^\S.*\b_\(", line) and not line.startswith(("def ", "class ", "#", "from ", "import ", '"', "'")):
                offenders.append(f"{path.relative_to(TUI_DIR)}:{number}")
    assert not offenders, offenders
