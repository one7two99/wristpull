# wristpull – notes for Claude Code

- Code, comments, commit messages, README: **English**. Conversation with the user: **German**.
- Python 3.14, `uv`, `src/wristpull/`, argparse CLI, stdlib `sqlite3`, `fitdecode`. Add dependencies only with a reason.
- Health data never enters the repo. Data lives in `~/Health/wristpull/` (config `[storage] data_dir`).
  Tests use synthetic FIT files built with `wristpull.fit.writer` (re-exported as `tests/fitwriter.py`); never commit real `.fit`/`.sqlite` files.
- Every new function gets a test. Before committing: `uv run pyflakes src tests && uv run pytest -q`.
- Tests never touch hardware: inject `FakeAdb` (tests/fake_adb.py) and `FakeMtp` (tests/fake_mtp.py), or pass `path=`
  for a directory; autouse fixtures in conftest block the real `Adb` and make the MTP tools answer "no device".
  Fixtures: `tests/fitwriter.py` (FIT), `tests/gb_db.py` (Gadgetbridge DB).
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
  dispatches on these kinds (`reparse.JSON_KINDS`). `summarizedActivities.json` (kind `activities_json`) names
  archived activities of any source (`activities.name`, migration; start within 120 s + `sport_matches`, only NULL
  is filled; display prefers `name` over `profile_name`), `MetricsMaxMetData_*` (kind `maxmet`) adds `vo2_max`
  metrics for days without a FIT value, `userBioMetrics.json` (kind `biometrics`) `weight_kg` metrics (grams → kg,
  last per day). `merge.merged_metric_by_day` feeds the Trends charts for weight and VO2max. Tests build synthetic
  exports with `tests/connect_export.py`.
- Workouts (`src/wristpull/workouts/`): one Markdown file per workout under `<data_dir>/workouts/` (grammar in
  `model.py`), FIT encoding in `fit/workout.py` on top of `fit/writer.py`, push/verify in `push.py` via
  `workouts/device.open_watch` (inject `mtp_factory`, or `path=` for a directory), SQL only in `store.py` (`workout_pushes`).
  Files on the watch are only added under `GARMIN/NewFiles/` (or `Workouts/`), never overwritten. The one deletion
  allowed on the device: `workouts/watch.py` removes top-level workout files in `GARMIN/Workouts/` or `NewFiles/` on explicit request,
  always after a backup to `<data_dir>/workouts/watch-backup/`; raw-data folders stay read-only. The fenix 6X Pro
  imports them from `NewFiles` at unplug (confirmed 2026-11-02) but stores only 20–25 workouts and silently drops
  new files above that; the schedule file (calendar date) is unverified.
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
  The Docker stack on Linux is the primary way to run wristpull; the native uv install is for development and for
  macOS/Windows (`docs/platforms.md`). There are no native systemd units or udev rules any more, only the Docker ones.
  `docs/history/` holds the original German work orders, unmaintained.
- Device agent (`src/wristpull/agent/`, `wristpull agent`, Compose service `agent`; concept: "Parity with the TUI" in
  `docs/dashboard-concept.md`): the only way the browser reaches the watch, the phone and the band. `agent/queue.py` is
  SQL only (tables `agent_jobs`, `agent_job_log`, `agent_state`); `agent/runner.py` claims one queued job at a time,
  runs the same library calls as `tui/jobs.py` with the fakes injected through `AgentDeps`, writes log lines and the
  result as JSON, and keeps a heartbeat plus the USB presence from sysfs. Job kinds are a fixed, validated list
  (`agent/kinds.py`); the web app only enqueues (`web/app.py` → `queue.enqueue`) and renders through `web/jobview.py`.
  `jobtext.py` holds the Textual-free result summaries shared by TUI and web. Secrets store `src/wristpull/secrets.py`
  (`<data_dir>/secrets.json`, 0600): the dashboard's Settings page writes the password hash, session secret, band key,
  API keys and Ollama address there; the store overrides `.env` (`secrets.apply` at CLI start, per agent job, per coach
  run); without any password hash the login accepts the start password `wristpull` once and forces a change
  (`web/app.py` `must_change`). The config file is mounted read-write into `web` (Settings saves it). The `agent` service has the rights of
  `cli` (USB, host network, D-Bus, AppArmor profile) and no ports; `web` still gets none of them. Tests drive
  `AgentRunner.run_once()` with `FakeMtp`/`FakeAdb`/`FakeTransport` and a temp sysfs; never the real devices.
- Watch over MTP (`src/wristpull/mtp.py`): libmtp CLI tools behind the `Mtp` protocol (`MtpTools`; exit codes mean
  nothing, stdout/stderr markers decide, fixtures in `tests/fixtures/mtp/`). The watch is always reached over MTP;
  `--path DIR` imports a directory instead (`LocalWatch`); there is no mount code (removed 2026-11-08). Import goes through `<data_dir>/staging/` (`importers.garmin.import_garmin_mtp`, name+size
  prefilter, then the SHA-256 check); workouts talk to the watch only via `workouts/device.py` (`WatchFiles`:
  `LocalWatch` / `MtpWatch`, `open_watch`). Tests inject `FakeMtp` (`tests/fake_mtp.py`) or `fake_mtp_runner`; an
  autouse fixture replaces the real tools with a "no raw devices" answer and clears `WRISTPULL_IN_CONTAINER`.
- Helio Strap over Bluetooth (`src/wristpull/zeppos/`, source `zepp_ble`, concept `docs/strap-ble-concept.md`): the Zepp OS
  protocol as Gadgetbridge speaks it; `ecdh.py` (B-163, pure Python), `aes.py` (pure Python, no dependency), `chunked.py`
  (codec), `protocol.py` (pure builders/parsers), `band.py` (async client over the `Transport` protocol), `transport.py`
  (bleak, extra `ble`, the only Bluetooth code), `fetch.py` (kinds, `WPZB` container, parsers reusing `gadgetbridge.py`),
  `store.py` (SQL only: `strap_sync`, replaced nights), `protobuf.py` (minimal wire decoder), `workout.py` (summary protobuf +
  BER-TLV details → `activities`/records/laps; the strap's auto-detected workouts are sport `auto_detected`).
  `importers/strap.py::pull_strap` asks each kind since
  `strap_sync` (activity from the local midnight of the last synced day: steps are cumulative per day), archives every
  answer as `raw/zepp_ble/<year>/zepp_<kind>/…` and keeps the data on the band; workouts come one per request (summary
  0x05, then details 0x06 for that start, `WORKOUT_ROUNDS` per pull) and are archived together (`pack_workout`). Merge `STRAP_PRIORITY = ("zepp_ble",
  "gadgetbridge_zepp")`. The auth key comes only from `WRISTPULL_STRAP_AUTHKEY` (`.env` for Docker, the shell natively, like
  the API keys; `runtime.STRAP_AUTHKEY_ENV`; tests set it with `monkeypatch.setenv`), never in the config, a file or the
  repo. Tests inject
  `FakeTransport` (`tests/fake_band.py`, plays the band behind the real codec) and synthetic buffers
  (`tests/zepp_buffers.py`); an autouse fixture blocks bleak. Never put real fetch buffers into the repository.
- Backup/restore (`src/wristpull/backup.py`, `wristpull backup create|show|restore`): one tar.gz (`wristpull-backup/` with
  `MANIFEST.json`, `env`, `config.toml`, `data/…`, `SHA256SUMS`) of the data directory (no `staging/`, database as a
  SQLite online-backup copy), the config file and `.env`. Restore extracts with `filter="data"`, keeps the archived modes
  (raw files stay 0444), verifies the checksums, rewrites only UID/GID and the two paths in `.env` and `[storage] data_dir`,
  deletes nothing (`--force` overwrites). Host side: `contrib/bin/wristpull-backup` (cli service, `.env` mounted read-only)
  and `contrib/bin/wristpull-restore` (plain `docker run` on a fresh clone, builds the image, writes `.env`). Tests build
  synthetic state in `tests/test_backup.py`; never put a real archive into the repo (`.gitignore` guards the name).
- Host scripts (`contrib/bin/`, POSIX sh or bash, shellcheck-clean): `wristpull` (wrapper), `wristpull-morning`,
  `wristpull-backup`/`wristpull-restore`, `wristpull-setup` (the guided first-run script: one function per step, all
  changes through `run()`/`sudo_run()`, `--dry-run`). `tests/test_contrib_scripts.py` checks syntax and drives the
  setup script with fake `docker`/`sudo`/`id` stubs on the PATH and `WRISTPULL_UDEV_DIR`/`WRISTPULL_APPARMOR_DIR`
  pointing at temp dirs; never let a test reach the real docker or sudo.
