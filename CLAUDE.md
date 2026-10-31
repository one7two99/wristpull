# wristpull – notes for Claude Code

- Code, comments, commit messages, README: **English**. Conversation with the user: **German**.
- Python 3.14, `uv`, `src/wristpull/`, argparse CLI, stdlib `sqlite3`, `fitdecode`. Add dependencies only with a reason.
- Health data never enters the repo. Data lives in `~/Health/wristpull/` (config `[storage] data_dir`).
  Tests use synthetic FIT files from `tests/fitwriter.py`; never commit real `.fit`/`.sqlite` files.
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
