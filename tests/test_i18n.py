from pathlib import Path

import pytest

from wristpull.tui import i18n

LOCALE = Path(i18n.LOCALE_DIR)
PO = LOCALE / "de" / "LC_MESSAGES" / "wristpull.po"
MO = LOCALE / "de" / "LC_MESSAGES" / "wristpull.mo"


@pytest.fixture(autouse=True)
def _reset_language():
    yield
    i18n.set_language("en")


def test_resolve_language_order():
    assert i18n.resolve_language("de", env={}) == "de"
    assert i18n.resolve_language(None, env={"LANG": "de_DE.UTF-8"}) == "de"
    assert i18n.resolve_language(None, env={"LANGUAGE": "fr:de", "LANG": "en_US"}) == "de"
    assert i18n.resolve_language(None, env={"LC_ALL": "C"}) == "en"
    assert i18n.resolve_language("xx", env={}) == "en"
    assert i18n.resolve_language(None, env={}) == "en"


def test_set_language_and_translate():
    assert i18n.set_language("de") == "de"
    assert i18n._("Overview") == "Übersicht"
    assert i18n.ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", 2).startswith("{n} Dateien")
    assert i18n.ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", 1).startswith("{n} Datei ")
    assert i18n.next_language() == "en"
    assert i18n.set_language("en") == "en"
    assert i18n._("Overview") == "Overview"
    assert i18n.set_language("klingon") == "en"


def test_every_extracted_msgid_has_a_german_translation():
    babel_extract = pytest.importorskip("babel.messages.extract")
    pofile = pytest.importorskip("babel.messages.pofile")
    extracted = set()
    for _filename, _line, message, _comments, _context in babel_extract.extract_from_dir(str(Path(i18n.__file__).parent)):
        extracted.add(message[0] if isinstance(message, tuple) else message)
    with PO.open("rb") as fh:
        catalog = pofile.read_po(fh, locale="de")
    translated = {m.id[0] if isinstance(m.id, tuple) else m.id for m in catalog if m.id and m.string and not m.fuzzy}
    missing = sorted(extracted - translated)
    assert not missing, f"untranslated msgids: {missing}"


def test_compiled_catalog_is_up_to_date():
    """Every entry of the .po must be present with the same text in the committed .mo."""
    import gettext

    pofile = pytest.importorskip("babel.messages.pofile")
    with PO.open("rb") as fh:
        catalog = pofile.read_po(fh, locale="de")
    with MO.open("rb") as fh:
        compiled = gettext.GNUTranslations(fh)._catalog  # type: ignore[attr-defined]
    stale = []
    for message in catalog:
        if not message.id or not message.string:
            continue
        if isinstance(message.id, tuple):
            for index, text in enumerate(message.string):
                if compiled.get((message.id[0], index)) != text:
                    stale.append(message.id[0])
        elif compiled.get(message.id) != message.string:
            stale.append(message.id)
    assert not stale, f"stale .mo, run: uv run pybabel compile -d src/wristpull/tui/locale -D wristpull; differs: {stale}"
