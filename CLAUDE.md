# wristpull – notes for Claude Code

- Code, comments, commit messages, README: **English**. Conversation with the user: **German**.
- Python 3.14, `uv`, `src/wristpull/`, argparse CLI, stdlib `sqlite3`, `fitdecode`. Add dependencies only with a reason.
- Health data never enters the repo. Data lives in `~/Health/wristpull/` (config `[storage] data_dir`).
  Tests use synthetic FIT files built with `wristpull.fit.writer` (re-exported as `tests/fitwriter.py`); never commit real `.fit`/`.sqlite` files.
- Every new function gets a test. Before committing: `uv run pyflakes src tests && uv run pytest -q`.
- Tests never touch hardware: inject `FakeAdb` (tests/fake_adb.py) and `roots`/`mounter`; an autouse
  fixture in conftest blocks the real `Adb`. Fixtures: `tests/fitwriter.py` (FIT), `tests/gb_db.py` (Gadgetbridge DB).
- Aggregation lives in `aggregate.py` (polars) on top of `merge.py`; export and report only render.
- After each wave: update README, tests green, commit.
- Raw files are immutable: SHA-256 dedup in `raw_files`, never overwrite, never delete on the device.
- All timestamps UTC ISO-8601 with `Z`; `tz` + `local_date` columns on every sample row.
- Undocumented Garmin FIT fields: check `FitCodeGenerator/src/main/resources/fit_profile.json` in the
  Gadgetbridge repo (Codeberg, use `git clone --sparse`, the web UI blocks fetches) before guessing.
- Verify assumptions about the fenix 6 with `wristpull inspect <file.fit>` once the watch is connected.
- TUI (`src/wristpull/tui/`, Textual, optional extra `tui`; ten screens, keys 1–9 and 0, order in `tui/nav.py`): only library calls, no SQL/FIT parsing
  (`tests/test_tui_boundary.py`). Strings via `_()` from `tui/i18n.py` at render time, never at module level;
  German catalog in `tui/locale/de/LC_MESSAGES/wristpull.po`, recompile with
  `uv run pybabel compile -d src/wristpull/tui/locale -D wristpull` (a test checks the `.mo`). Long jobs go
  through `tui/jobs.py` (worker thread, own connection, cooperative `stop` polled between files). Tests: `App.run_test()` + `tests/tui_helpers.py`,
  inject `TuiDeps` (FakeAdb, `garmin_tree`, fixed `today`); every TUI test module starts with
  `pytest.importorskip("textual")`.
- After changing a parser: `uv run wristpull reparse --source ... | --kind ... | --all` replaces the rows of
  archived files (raw files are never re-fetched). Gadgetbridge's own tables in the archived `Gadgetbridge.db`
  are the reference for cross-checking strap and Garmin-via-Gadgetbridge values.
- Coach (`src/wristpull/coach/`): backends behind `Backend` (Ollama/OpenRouter via stdlib HTTP, Claude via the
  `anthropic` SDK in the optional extra `coach`, default model `claude-opus-5-5`, no `temperature`); metrics and
  flags are computed in Python before prompting; SQL only in `store.py`/`context.py`; every test uses `FakeBackend`
  or fake transports, never the network. Goal profile and dry-run prompts live under `<data_dir>/coach/`.
  Concept and waves: `docs/coach-concept.md`.
- Workouts (`src/wristpull/workouts/`): one Markdown file per workout under `<data_dir>/workouts/` (grammar in
  `model.py`), FIT encoding in `fit/workout.py` on top of `fit/writer.py`, push/verify in `push.py` via
  `importers.garmin.locate_garmin` (inject `roots`/`mounter`), SQL only in `store.py` (`workout_pushes`).
  Files on the watch are only added under `GARMIN/NewFiles/` (or `Workouts/`), never overwritten. The one deletion
  allowed on the device: `workouts/watch.py` removes top-level workout files in `GARMIN/Workouts/` or `NewFiles/` on explicit request,
  always after a backup to `<data_dir>/workouts/watch-backup/`; raw-data folders stay read-only. The fenix 6X Pro
  imports them from `NewFiles` at unplug (confirmed 2026-11-02) but stores only 20–25 workouts and silently drops
  new files above that; the schedule file (calendar date) is unverified. Writes go through `gio copy` on gvfs MTP.
  Names are unique per ISO week (generated names end with `dd.mm.`; `model.mark_name_clashes` makes duplicates invalid).
