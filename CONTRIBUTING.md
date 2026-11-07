# Contributing

Thanks for looking. A few rules keep the project safe to publish and easy to change.

- **No health data in the repository.** Tests use synthetic FIT files built with `wristpull.fit.writer` and a
  synthetic Gadgetbridge database from `tests/gb_db.py`, a synthetic Garmin Connect export from
  `tests/connect_export.py`, the watch over MTP through `tests/fake_mtp.py`. Never commit `.fit`, `.sqlite`, `.db` or `.gpx`
  files, screenshots with real values, serial numbers or MAC addresses. Use `<serial>`, `<id>`,
  `AA:BB:CC:DD:EE:01` and the like.
- **Every function gets a test.** Run `uv run pyflakes src tests && uv run pytest -q` before a pull
  request. TUI tests run headless; coach tests use `FakeBackend`, never the network; web tests use
  Starlette's `TestClient` (`uv sync --extra web`).
- **The web dashboard** (`src/wristpull/web/`) only calls library functions (no SQL, no FIT parsing),
  keeps secrets in the environment, never in files, and runs exactly one uvicorn worker. Never commit
  a `.env`; `.env.example` holds placeholders only.
- **Raw files are immutable, parsers are not.** When you change a parser, run `wristpull reparse` on
  your own archive and compare before and after; mention the result in the pull request.
- **Undocumented Garmin fields**: check `fit_profile.json` in the Gadgetbridge repository before
  guessing; cite the message and field numbers in the commit message.
- Code, comments, commits and docs are in English. TUI and web dashboard share German and English catalogs; new
  strings go through `_()` and into `src/wristpull/tui/locale/de/LC_MESSAGES/wristpull.po`
  (`uv run pybabel compile -d src/wristpull/tui/locale -D wristpull`).
- Small, focused pull requests with a sentence on what you verified against a real device are the
  easiest to merge.
