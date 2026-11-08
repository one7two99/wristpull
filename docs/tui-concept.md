# wristpull TUI – concept

Status: implemented (MVP 2026-10-31, second wave 2026-11-01: activities, sleep, export & report form, editable settings, cooperative cancel; coach screen 9 and workouts screen 0 followed on 2026-11-02). Snapshot tests remain open. The text mockups that guided the build were removed after the screens existed; `screenshots/` (next to this file) holds renders of the real TUI.

## Why

After three waves wristpull has a complete CLI (`import garmin`, `pull phone`, `status`, `inspect`,
`config`, `export`, `report`) and a SQLite database with about twenty tables. Everything is reachable
only through individual commands and `sqlite3`. The TUI should

1. run the important commands comfortably (options, live log, result),
2. give a quick look at the data (days, activities, sleep, source coverage),
3. open the relevant files (raw archive, exports, reports, config),
4. support German and English, switchable at runtime.

## Guiding principles

- **The TUI is a shell, not a second implementation.** It calls only the existing library functions
  (`import_garmin`, `pull_phone`, `collect_status`, `daily_frame`, `merged_*`, `export_for_claude`,
  `write_report`, `decode_fit`/`summarize`). No SQL and no parsing in the TUI. When a query is
  missing (hourly heart rate of one day, for example) it becomes a tested function in a library
  module, so CLI, MCP server and the Ollama coach can use it too.
- **Everything the TUI does, the CLI can do.** Every action displays its CLI equivalent
  (`wristpull pull phone --wait 90`) so it can be copied into systemd or cron.
- **Never block.** Import and pull run in worker threads with a live log; the UI stays responsive and
  a second job is refused while one is running.
- **Keyboard first, mouse works too.** Digits switch screens, letters trigger actions, `/` opens the
  command palette, `?` the help overlay, `F12` toggles the language.
- **Readable at 100×30, usable from 80×24** (narrow columns collapse).
- **Health data stays local.** The TUI writes nothing into the repository; screenshots and tests use
  synthetic values only.

## Screens

A ninth screen, **9 Coach**, was added with the training-coach concept (`coach-concept.md`): it runs the
daily check, the weekly review and questions through the job runner, lists the runs with tokens, cost and
rating, shows the answer, and hosts the goal wizard and the profile editor. A tenth, **0 Workouts**
(2026-11-02), lists the Markdown workout files, previews the steps the way the watch shows them,
opens them in `$EDITOR` and runs push/verify against the watch (MTP) as jobs.

```
            ┌──────────────┐
            │ 1 Overview   │  status, 7-day KPIs, recent activities, notes
            └──────┬───────┘
   ┌───────┬───────┼───────┬────────┬────────┬────────┬────────┐
   ▼       ▼       ▼       ▼        ▼        ▼        ▼        ▼
 2 Devices 3 Days 4 Activ. 5 Sleep 6 Files 7 Export 8 Settings ? Help
 & Sync                                      & Report
   │       │       │                 │
   │       │       └─► detail: laps, HR zones, HR curve, open FIT
   │       └─► day detail: hourly HR, stress / body battery, the night's sleep
   │                                 └─► inspect FILE (messages, fields, samples)
   └─► job log (import garmin / pull phone), result, CLI equivalent
```

| # | Screen | Purpose | CLI equivalent |
|---|--------|---------|----------------|
| 1 | Overview | Device status, last import per source, KPIs of the last 7 days with sparklines and week-over-week delta, recent activities, notes (unverified mappings, empty folders, KDE MTP hint) | `status` |
| 2 | Devices & Sync | Set options and start: Garmin import (path, dry run), phone pull (serial, trigger, sync, wait, dry run); live log; result table (found/new/skipped/errors, rows per table); error files open in inspect | `import garmin`, `pull phone`, `import gadgetbridge` |
| 3 | Days | `daily_summary` over a period (week/month/custom), source toggle (merged / garmin_usb / gadgetbridge_garmin / strap), column picker, sorting; detail pane for the selected day | export data, so far only via sqlite3 |
| 4 | Activities | List with filters (sport, period, source); detail: laps, time in HR zones, HR curve, open raw file | so far only via sqlite3 |
| 5 | Sleep | Nights with duration, stage bar (light/deep/REM/awake), HRV, SpO2, respiration; Garmin and strap side by side | so far only via sqlite3 |
| 6 | Files | Tree of `raw/<source>/<year>/<kind>/`, `exports/`, `reports/`, config; details (SHA, status, size, message types); actions: inspect, copy path, open with `xdg-open`/`$EDITOR`, Markdown preview | `inspect FILE`, `config` |
| 7 | Export & Report | Form: period (presets last week, last month, custom), kind (Claude export / weekly / monthly report), charts, CSV, output dir; result with file list and Markdown preview; copy path for chat | `export --for-claude`, `report week|month` |
| 8 | Settings | Config fields (data_dir, timezone, garmin_folders, phone_serial, export dir), new `[ui]` section (language, theme, default range), save writes `config.toml`; log view | `config --init`, editor |
| 9 | Coach | Runs table (tokens, cost, rating), answer, question input, journal note, goal wizard, profile editor, `t` drafts workouts from the last weekly plan | `coach daily|weekly|ask|rate|note|profile` |
| 0 | Workouts | Workout files of the period with status, preview (steps + Markdown), new from template (`$EDITOR`), edit, drafts from the plan, push selected / upcoming / force, dry run, verify, delete; `w` switches to the workouts on the watch (count against the limit, preview, remove with backup) | `workout new|list|check|push|verify|watch|remove|from-plan` |
| ? | Help | Key bindings, version and path info, link to docs/gadgetbridge.md | `--version` |
| / | Command palette | Every action and screen via fuzzy search (built into Textual) | – |

## Features by priority

**MVP (one session)**
- App skeleton, header/key bar, screens 1, 2, 3, 6 (files: archive + inspect only), help, palette.
- i18n de/en, language from config or `LANG`; runtime switch may follow in the second wave.
- Job runner for `import garmin` and `pull phone` with live log, result, CLI equivalent.
- Tests: headless with `App.run_test()`, `FakeAdb` and the `garmin_tree` fixture.

**Second wave**
- Screens 4 (activities with detail), 5 (sleep), 7 (export & report with preview), 8 (settings).
- New read-only helpers in `detail.py`: hourly heart rate of a day, sleep stages of one night,
  activity records for charts, source coverage per day.
- After a real night: verification panel ("mapping confirmed / open") driven by the `meta` table.

**Backlog (proposals)**
- **Watch mode**: detect a newly attached fenix (USB) or phone (`adb devices` poll)
  and offer the import with one key; later udev/systemd.
- **Data quality panel**: gaps (days without data per source), duplicates, error files, days with 0
  steps (the known midnight carry-over).
- **Source comparison**: diff `garmin_usb` vs `gadgetbridge_garmin` vs strap for one day (steps,
  resting HR, sleep) as a table, to build trust in the merge rule.
- **Coach hook**: realised as the `coach` module and CLI (see `coach-concept.md`); the TUI screen for it is
  wave C3 of that concept.
- **Calendar heatmap** (intensity minutes or steps per day, GitHub style) on the overview.
- **Read-only SQL console** (`SELECT` enforced) with table/column completion for ad-hoc questions.
- **Desktop notification** after a finished import (`notify-send`), optional.
- **Push to data repo**: "commit to private data repo" on the `exports/` path once that repo exists.
- **Nutrition** later as its own source (`mynetdiary`, `manual`) as columns on the Days screen.

## Technical design

### Framework and packaging
- **Textual** (8.x). Reasons: ready-made widgets (`DataTable`, `TabbedContent`, `DirectoryTree`,
  `RichLog`, `Markdown`, `Sparkline`), thread workers with thread-safe `post_message`, headless test
  harness (`App.run_test()` + `Pilot`), CSS layout. urwid, prompt_toolkit or curses would mean
  rebuilding tables, threading and tests by hand.
- **Optional extra, not a core dependency**: `[project.optional-dependencies] tui = ["textual>=8"]`,
  installed with `uv sync --extra tui`. The systemd/cron path (`import`, `pull`) stays lean. The dev
  group gains `babel` for the translation catalogs.
- **Entry point**: subcommand `wristpull tui` in `cli.py` with a lazy `from .tui.app import run`; on
  `ImportError` it prints "install with: uv sync --extra tui" and exits 1. One binary, `--config`
  keeps working.

### Package layout and boundary rule
```
src/wristpull/tui/
  app.py           WristpullApp(App): config, TuiDeps, JobRunner, screen stack, theme
  deps.py          TuiDeps: adb_factory, mtp_factory, sleep, today, open_path, refresh_interval
  jobs.py          JobSpec, JobRunner (start, lock, result messages)
  queries.py       adapter library call -> rows for widgets (no SQL)
  i18n.py          _, ngettext, set_language, resolve_language
  styles.tcss
  screens/         home, sync, days, activities, sleep, files, export, settings, coach (+ coach_wizard), workouts, help
  widgets/         job_log (RichLog + busy), frame_table (polars DataFrame -> DataTable), kpi, keybar
  locale/wristpull.pot, locale/de/LC_MESSAGES/wristpull.{po,mo}
```
- `wristpull.tui` imports only `config`, `db.connect`, `status`, `aggregate`, `merge`, `export`,
  `report`, `importers.*`, `fit.decode`, `adb.AdbError`, `detail`, `coach.*` and `workouts.*`. No `sqlite3`, no
  `conn.execute`, no `fitdecode`. Enforced by `tests/test_tui_boundary.py` (regex scan).
- New read-only helpers go into **`src/wristpull/detail.py`** (not `aggregate.py`, which stays
  about date-range frames): `hourly_heart_rate(conn, local_date, tz)`, `day_detail(conn, local_date)`,
  `sleep_night(conn, sleep_id)`, `activity_detail(conn, activity_id)`. Tests in `tests/test_detail.py`
  via `garmin_tree` + `import_garmin`.

### Long jobs without freezing
- `JobRunner.start(spec) -> bool` refuses while `active` is set (toast via `app.notify`). No
  `exclusive=True`: that only pretends to cancel a thread.
- `run_worker(partial(self._run, spec), thread=True, group="job")`. The worker opens **its own
  connection** `connect(config.db_path)` and closes it in `finally`; the UI thread keeps its own.
  WAL allows reading while the import writes.
- `log=` becomes `lambda msg: self.post_message(JobLine(job_id, msg))` (thread-safe). Completion
  via `Worker.StateChanged`: SUCCESS renders `ImportStats`/`PullStats`, ERROR shows `AdbError`
  and friends, then `DataChanged` is posted and screens reload.
- Devices come from `TuiDeps`: `pull_phone(adb=deps.adb_factory(serial), sleep=deps.sleep)`,
  `import_garmin(mtp_factory=deps.mtp_factory)`. The autouse guard in conftest remains
  the safety net.
- Cancel (second wave, cooperative): a `sleep=` wrapper that aborts when
  `get_current_worker().is_cancelled`, plus an optional `check_cancel` parameter in
  `import_garmin`/`pull_phone` between files. A running `adb pull` finishes first. MVP: no cancel,
  buttons disabled while busy.

### i18n
- **stdlib `gettext`**, source language English, domain `wristpull`, catalog under
  `tui/locale/de/LC_MESSAGES/`. The `.po` is the source, the `.mo` is committed (small, no build
  hook; check `.gitignore`).
- Tooling: `babel.cfg` in the repo, `uv run pybabel extract/update/compile`.
- `i18n.py`: `_()` and `ngettext()` delegate to a module-level `Translations`; `set_language(lang)`
  loads via `gettext.translation(..., fallback=True)`; no `gettext.install()`. Resolution order in
  `resolve_language`: `config.ui_language` → `LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG` → English.
- Rule: call `_()` at compose/render time, never at module level (`BINDINGS` are evaluated at
  import) → own `KeyBar` widget instead of `Footer`.
- Runtime switch (`F12`): `set_language` + `recompose()` over the screen stack. The MVP reads the
  language at start; switching lands in the second wave.
- Plurals: `ngettext("%(n)d new file", "%(n)d new files", n)`, `.po` header
  `nplurals=2; plural=(n != 1)`.
- Tests `tests/test_i18n.py`: (1) every extracted msgid has a non-empty, non-fuzzy `msgstr` in the
  German catalog; (2) the `.mo` matches the compiled `.po` (stale binaries fail); (3)
  `resolve_language` with a patched environment.
- Rejected: TOML/JSON dictionary. Zero tooling, but no completeness check, no plurals, keys instead
  of readable text in the code.

### Tests
- `WristpullApp(config, deps)`; tests inject `TuiDeps(adb_factory=FakeAdb, mtp_factory=FakeMtp,
  sleep=no-op, today=fixed, open_path=recorder, refresh_interval=0)`. The app
  never calls `load_config()` or `date.today()` itself.
- `async with app.run_test(size=(120, 40)) as pilot: await pilot.press("i"); await
  app.workers.wait_for_complete()`, then assert on widget state (`DataTable.row_count`, log lines).
  Async through a small `run(coro)` helper; `pytest-asyncio` only if needed.
- Every TUI test module starts with `pytest.importorskip("textual")` so the suite stays green
  without the extra.
- Snapshot tests (`pytest-textual-snapshot`): **not yet**. SVGs change with every Textual release
  and the UX is still moving. Later for two or three core screens.
- Test files: `test_tui_app.py`, `test_tui_jobs.py`, `test_tui_boundary.py`, `test_detail.py`,
  `test_i18n.py`, additions to `test_config.py` and `test_cli.py` (`tui` without textual → exit 1).

### Config
- `Config` gains `ui_language: str | None = None`, `ui_theme: str = "textual-dark"`,
  `ui_range_days: int = 28`; `load_config` reads `[ui]` with `.get`, so old files without `[ui]`
  keep working. `EXAMPLE_CONFIG` and `render()` get the `[ui]` block.
- The settings screen is read-only in the MVP; writing back (second wave) goes through
  `Config.render()` after confirmation, no `tomlkit`.

### Implementation order
**MVP, one session**
1. `pyproject.toml`: extra `tui`, `babel` in dev, `babel.cfg`.
2. `config.py` `[ui]` + tests.
3. `cli.py` subcommand `tui` with lazy import + test.
4. `tui/i18n.py`, catalog `de`, `tests/test_i18n.py`.
5. `tui/deps.py`, `tui/jobs.py`, `widgets/job_log.py`, `tui/app.py`, `styles.tcss`.
6. Screens `home` (collect_status, KPIs from `daily_frame`, actions), `sync` (forms + log),
   `data` (Days tab from `daily_frame`, range from `ui_range_days`), `files` (`DirectoryTree` over
   `raw/`, `exports/`, `reports/`, open via `deps.open_path`), `config_view` (read-only).
7. `test_tui_app.py`, `test_tui_jobs.py`, `test_tui_boundary.py`; README section "TUI"; commit.

**Second wave**: `detail.py` + detail panes (hourly HR sparkline, sleep stages, activity records),
`screens/inspect.py` (`summarize()` as a `Tree`), export/report form with Markdown preview, writable
settings, runtime language switch, cooperative cancel, snapshot tests.
