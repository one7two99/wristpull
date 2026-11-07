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
- Garmin Connect account export (`src/wristpull/importers/connect.py`, source `garmin_connect`, lowest merge priority):
  the portal zip is read in place (inner `UploadedFiles_*.zip` from memory), every FIT goes through
  `importers.garmin.import_file(kind=None, …)` which derives the kind from the FIT type (`FIT_TYPE_KINDS`); the
  `<e-mail>_` prefix of the file names is stripped. JSON parsers live in `garmin_connect.py` (every field
  optional, placeholders before 2013 discarded and counted): UDS daily summaries → table `connect_daily`
  (kind `uds`, `INSERT OR REPLACE` per day, later blocks win), nights → `sleep` rows without stages (kind
  `sleep_json`); `daily.rebuild_daily_summary` overlays `connect_daily` on NULL columns only. `reparse`
  dispatches on these kinds (`reparse.JSON_KINDS`). Tests build synthetic exports with
  `tests/connect_export.py`. Phase 3 (activity names, VO2max) per `workorders/garmin-connect-import.md`.
- Workouts (`src/wristpull/workouts/`): one Markdown file per workout under `<data_dir>/workouts/` (grammar in
  `model.py`), FIT encoding in `fit/workout.py` on top of `fit/writer.py`, push/verify in `push.py` via
  `importers.garmin.locate_garmin` (inject `roots`/`mounter`), SQL only in `store.py` (`workout_pushes`).
  Files on the watch are only added under `GARMIN/NewFiles/` (or `Workouts/`), never overwritten. The one deletion
  allowed on the device: `workouts/watch.py` removes top-level workout files in `GARMIN/Workouts/` or `NewFiles/` on explicit request,
  always after a backup to `<data_dir>/workouts/watch-backup/`; raw-data folders stay read-only. The fenix 6X Pro
  imports them from `NewFiles` at unplug (confirmed 2026-11-02) but stores only 20–25 workouts and silently drops
  new files above that; the schedule file (calendar date) is unverified. Writes go through `gio copy` on gvfs MTP.
  Repeat blocks are flat as in FIT (block steps, then a `Step("repeat", repeat=N, repeat_from=i)`;
  `expand_steps` for totals). Names are unique per ISO week (generated names end with `dd.mm.`; `model.mark_name_clashes` makes duplicates invalid).
- Web dashboard (`src/wristpull/web/`, Starlette + Jinja2 + vendored htmx/ECharts, optional extra `web`; concept
  `docs/dashboard-concept.md`): only library calls, no SQL/FIT parsing (test in `tests/test_web.py`); page data in
  `web/pages.py`; strings share the TUI catalog but are translated per request (`web/i18n.py`). Secrets only from the
  environment (`WRISTPULL_WEB_PASSWORD_HASH`, `WRISTPULL_WEB_SECRET`). Exactly one uvicorn worker (in-process rate limit
  and jobs) – never add a workers option. Docker: `Dockerfile`, `compose.yaml`, `deploy/Caddyfile`, `.env.example`.
  Never suggest `OLLAMA_HOST=0.0.0.0`. Tests use Starlette's `TestClient` with `base_url="https://testserver"`.
  Coach runs go through `web/jobs.py` (inject `JobRegistry(threaded=False)` and `WebDeps.coach_backend` in tests);
  workout files are written only via `workouts/store.write_text` (bare names, nothing outside the folder).
  Rebuild a running stack with `docker compose build web && docker compose up -d web` (Caddy keeps its CA).
- Docker `cli` service (concept `docs/docker-concept.md`): one image (`wristpull:latest`) for `web` and `cli`; USB
  pass-through (`c 189:*`), host network and the adb server only in `cli`, never in `web`, never `privileged`.
  Inside the container `WRISTPULL_IN_CONTAINER=1`, `WRISTPULL_DATA_DIR=/data` (overrides `[storage] data_dir`,
  `Config.file_data_dir` keeps the file's value), `WRISTPULL_CONFIG_FILE` names the mounted config; helpers in
  `runtime.py`. adb keys live in `<data_dir>/adb`. Host-side files (udev, units, wrapper) go under `contrib/`.
- Watch over MTP (`src/wristpull/mtp.py`): libmtp CLI tools behind the `Mtp` protocol (`MtpTools`; exit codes mean
  nothing, stdout/stderr markers decide, fixtures in `tests/fixtures/mtp/`). `[garmin] access = auto | gvfs | mtp`
  (`mtp.resolve_access`). Import goes through `<data_dir>/staging/` (`importers.garmin.import_garmin_mtp`, name+size
  prefilter, then the SHA-256 check); workouts talk to the watch only via `workouts/device.py` (`WatchFiles`:
  `LocalWatch` / `MtpWatch`, `open_watch`). Tests inject `FakeMtp` (`tests/fake_mtp.py`) or `fake_mtp_runner`; an
  autouse fixture blocks the real tools and clears `WRISTPULL_IN_CONTAINER`.
