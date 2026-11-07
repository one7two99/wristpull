# wristpull web dashboard – concept

Status: concept approved 2026-11-03; D1 implemented the same day (`wristpull serve`, login with
scrypt hash, signed session, CSRF, rate limit, pages Today and Trends, Dockerfile, Compose, Caddy); D2 the same day (Activities and Sleep with
detail pages); D3 the same day (coach runs as background jobs, rating, journal, workout list, templates,
drafts from the plan, Markdown editor with live check).
Implementation waves at the end.

## Why

wristpull has a CLI, a terminal UI and a coach, but nothing to look at on the phone. The
dashboard is a small web app in a Docker container that shows the same numbers as CLI and TUI
and lets the user trigger the coach, rate its answers, write journal notes and edit workouts
from the browser. Decisions taken with the user (2026-11-03):

- Reachable **from the phone in the home network**, protected by **HTTPS and a password**
  (Caddy with a local certificate authority).
- **Actions** in the browser, not only charts: coach runs, rating, journal, workout files.
- Pulling the watch (USB/MTP), the phone (ADB) and pushing workouts to the watch **stay on the
  laptop** (CLI, TUI, systemd/udev). The container has no USB access; the dashboard shows when
  the last sync happened.
- Runs **on this laptop first**, later on a separate server laptop (see "Moving to a server").

## Guiding principles

- **Same numbers everywhere.** The web layer only calls library functions; no SQL and no FIT
  parsing in `src/wristpull/web/` (enforced by a boundary test like `tests/test_tui_boundary.py`).
  A query that is missing goes into the library module where it belongs.
- **Nothing leaves the machine at runtime.** htmx and ECharts are vendored under
  `web/static/vendor/` (pinned versions); no CDN, no web fonts, no map tiles. Only coach runs
  talk to the chosen LLM backend, exactly as from the CLI.
- **Mobile first.** One column on the phone, a grid from tablet width, light and dark theme.
- **No build step.** Server-rendered HTML (Jinja2), htmx for actions and partial updates,
  ECharts for charts. No Node.
- **Missing is not zero.** Gaps are drawn as gaps (null in the chart series), not as zero.

## Architecture

```
src/wristpull/web/
  app.py        Starlette app factory create_app(config, deps) -> app; routes, templates, static
  auth.py       password hash check (scrypt, stdlib), signed session cookie, CSRF token, login rate limit
  jobs.py       in-process job registry: one background thread, one job at a time, own DB connection
  views.py      page handlers: today, trends, activities, sleep, coach, workouts
  series.py     daily_frame columns -> JSON series for ECharts (None -> null)
  i18n.py       per-request gettext translation on the TUI catalog (tui/locale, domain wristpull)
  templates/    Jinja2: base.html, partials for htmx swaps
  static/       app.css, app.js, vendor/htmx.min.js, vendor/echarts.min.js
```

- Optional extra `web`: `starlette`, `uvicorn`, `jinja2` (`httpx` in the dev group for the
  test client). CLI: `wristpull serve [--host H] [--port P] [--data-dir D] [--hash-password]`,
  imported lazily like `wristpull tui`. `--data-dir` overrides `config.data_dir` (`/data` in
  the container).
- **DB access:** `db.connect()` per request and per job thread (sqlite3 connections are
  thread-bound). The database runs in WAL mode, so the container and the CLI on the laptop can
  read and write at the same time; the bind mount sits on the same kernel, so WAL locking works.
- **Status without hardware:** `status.collect_status(..., roots=lambda: [], mtp_urls=lambda: [])`
  so the container never probes USB.
- **i18n:** the TUI's catalog and domain, but a `gettext.translation(...)` per request instead of
  the process-global `set_language`, so two browsers can use different languages.
- `tui/jobs.py` imports Textual and is not used; `web/jobs.py` follows its pattern
  (`JobRunner._work`: own connection, closed in `finally`, cooperative stop).

### Exactly one worker

The job registry and the login rate limit live in process memory. A second uvicorn worker would
see neither (a job started in one worker would be invisible to the status poll served by the
other, and the rate limit could be bypassed by spreading attempts). uvicorn therefore runs with
**exactly one worker**: hard-wired in `wristpull serve` (no option for it) and in the
Dockerfile `CMD`, with a comment in Dockerfile and Compose file. One worker is plenty for one
user; coach runs happen in the background thread and do not block requests.

## Library functions reused

| Page | Functions |
|---|---|
| Today | `tui/queries.day_rows` (= `aggregate.daily_frame` as dicts), `coach/metrics.baseline`, `deltas`, `acute_chronic`, `recovery_flags`, `coach/store.recent_runs`, `status.collect_status` |
| Trends | `aggregate.daily_frame`, `weekly_frame`, `period_overview`, `week_bounds`, `month_bounds`, `coach/metrics.acute_chronic` |
| Activities | `detail.activities`, `sports`, `activity_detail` (records → HR curve via `downsample`, `hr_zones_s`) |
| Sleep | `detail.nights`, `sleep_night` (stages, HRV, SpO2), `merge.merged_sleep` (watch vs strap) |
| Coach | `coach/runner.run_coach` + `coach/backends.make_backend` (as a job), `coach/store.recent_runs`, `get_run`, `add_feedback`, `add_note`, `recent_notes`, `coach/profile.load_profile` |
| Workouts | `workouts/store.list_workouts`, `load`, `save`, `push_status`, `mark_folder_clashes`, `workouts/model.parse_markdown`, `describe_steps`, `template`, `workouts/drafts.drafts_from_plan` |

The chart series are the `daily_frame` columns that `report.build_report` already feeds to
`charts.py` (steps, resting HR watch and strap, sleep hours, stress, body battery), plus HRV and
training load.

## Pages

1. **Today** – tiles for resting HR, HRV, sleep, stress, steps and body battery, each against the
   user's 28-day baseline; recovery flags; the latest coach daily check (excerpt, rating
   buttons); "last sync: watch … / phone …".
2. **Trends** – 4, 12, 52, 104, 260 or 520 weeks (beyond a year aggregated per ISO week): resting HR (watch and strap), HRV, sleep hours and score,
   stress, weekly volume (hours, km), acute:chronic workload ratio with the 0.8–1.3 band;
   weight and VO2max as point series when the window holds values (`merge.merged_metric_by_day`,
   fed by the Garmin Connect export and the watch's metrics files).
3. **Activities** – list with sport filter; detail with HR curve, zone minutes, laps.
4. **Sleep** – nights list; detail with a stage band, HRV, SpO2, watch vs strap.
5. **Coach** – runs with sections, rating (−1/0/+1 with note), "run daily / weekly / ask" as a
   background job with live status, journal note for today.
6. **Workouts** – files of the coming weeks with status (draft, pushed, verified, removed);
   editor for the Markdown with a live check (steps as the watch shows them, name clashes);
   "drafts from plan", "new from template". Push stays on the laptop
   (`wristpull workout push --upcoming`); the page says so.

## Security

- **Password:** only its hash comes from the environment (`WRISTPULL_WEB_PASSWORD_HASH`, created
  with `wristpull serve --hash-password`, scrypt from `hashlib`). Never in config files.
- **Session secret:** `WRISTPULL_WEB_SECRET` from the environment, so sessions survive a
  container restart and the phone stays logged in. Required in Compose
  (`${WRISTPULL_WEB_SECRET:?…}`, the start fails without it). A local `wristpull serve` without
  it generates a random secret per start and warns that sessions end with the process.
  Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Cookie `HttpOnly`, `Secure`, `SameSite=Strict`; a CSRF token on every POST (htmx sends it as a
  header); login rate limit (in process, hence one worker); session lifetime 30 days.
- **TLS:** a Caddy container with `tls internal` (its own local CA) terminates HTTPS on port
  8443 of the laptop's LAN address; the app container listens only inside the Compose network.
  The README explains how to install Caddy's root certificate on Android once.
- API keys (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`) via `env_file`, as for the CLI.
- The container runs as the user's UID/GID; `~/Health/wristpull` is bind-mounted read-write
  (actions write), the config file read-only.

### Ollama

The container reaches Ollama on the host as `host.docker.internal` (`extra_hosts:
host-gateway`, which is the docker0 address). Ollama listens on `127.0.0.1` by default, which
the container cannot reach. **`OLLAMA_HOST=0.0.0.0` is not allowed**: it opens Ollama to the
whole LAN, without authentication. One of these instead:

1. Bind Ollama to the docker0 address (`OLLAMA_HOST=172.17.0.1`). Consequences: the coach on
   the host must use `http://172.17.0.1:11434` too (`[coach] ollama_url`), and the Ollama
   service needs `After=docker.service`, because docker0 does not exist before Docker starts.
2. Or keep Ollama as it is and add a firewall rule (nftables/ufw) that allows port 11434 only
   from the Docker subnets, with Ollama bound to an address the containers can reach.

## Moving to a server

Later the dashboard moves to a separate server laptop. The watch and the phone stay attached to
the work laptop. Problem: imports write on the laptop, web actions write on the server – two
writable copies of one SQLite database would diverge, and syncing a live WAL database with
Syncthing or rsync can corrupt it.

**Direction: the server becomes the only database.** The laptop pulls the raw files from watch
and phone as today, then copies the **raw files** (not the database) to the server over
SSH/rsync, and the server imports them from files (`import_garmin(conn, config, path=…)` and the
Gadgetbridge database import). The database is never copied while it is being written. Pushing
workouts to the watch stays on the laptop (the watch is attached there); it fetches the workout
Markdown files from the server first. Implemented in a later wave, after D3.

Rejected:

- Syncthing/rsync of the live database – risk of corruption, and conflicts when both sides write.
- Two databases merged regularly – conflicting row ids and duplicate imports.
- Laptop stays the master and copies a snapshot to the server – web actions on the server
  (ratings, notes, coach runs, workout edits) would be overwritten.
- SQLite on an NFS/SMB share – file locking over network file systems is unreliable.

## Docker

- `Dockerfile`: `python:3.14-slim`, `uv sync --frozen --extra web --extra coach --extra tui --no-dev`,
  `adb`, `mtp-tools`, `nano`, non-root user, `CMD` with one uvicorn worker on `0.0.0.0:8000` inside the
  Compose network. One image for both services.
- `compose.yaml`: `web` (data and config mounts, `env_file: .env`, `extra_hosts: host-gateway`,
  healthcheck, no USB), `caddy` (ports `8443:8443`, volume for its CA) and `cli` (profile `cli`, USB
  pass-through, host network, started per command; see `docker-concept.md`).
- `deploy/Caddyfile`, `.env.example` (secret, password hash, API keys, UID/GID); `.env` is git-ignored.

## Testing

Starlette's `TestClient` on synthetic data (`config`, `conn`, `garmin_tree` imported with
`import_garmin(..., path=garmin_tree)`, more days from the `build_*_fit(day)` builders):
login required, wrong password, rate limit, missing CSRF token → 403, every page renders,
actions go through the library (coach with `FakeBackend`), boundary test for `web/`, `serve`
refuses more than one worker and warns without a secret. No test touches the network or
hardware.

## Waves

- **D0** this concept + a clickable mockup with sample data → approval.
- **D1** skeleton: `serve`, login/secret/CSRF/rate limit, layout, Today and Trends, Docker and Caddy.
- **D2** Activities and Sleep.
- **D3** actions: coach runs and rating, journal, workout editor, job status.
- Later: moving to the server (raw-file transfer + import on the server).

Each wave: README updated, tests green, commit.
