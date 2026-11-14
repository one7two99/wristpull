# wristpull web dashboard – concept

Status: concept approved 2026-11-03; D1 implemented the same day (`wristpull serve`, login with
scrypt hash, signed session, CSRF, rate limit, pages Today and Trends, Dockerfile, Compose, Caddy); D2 the same day (Activities and Sleep with
detail pages); D3 the same day (coach runs as background jobs, rating, journal, workout list, templates,
drafts from the plan, Markdown editor with live check).
Implementation waves at the end. The parity plan of 2026-11-13 (every TUI function in the browser,
device jobs through the `agent` service) is the section "Parity with the TUI". Charts, metric pages
and the analysis layer (readiness, correlations, AI insights report) have their own concept since
2026-11-14: [analysis-concept.md](analysis-concept.md); its waves interleave with the parity waves.

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
  the last sync happened. *Revised 2026-11-13:* the `web` container still has no device access,
  but a separate `agent` service runs these jobs on its behalf, so the browser can start them
  (see "Parity with the TUI").
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
   user's 28-day baseline with a 14-day sparkline and a link to the metric page; the day's Body
   Battery and stress curve; recovery flags; the latest coach daily check (Markdown, rating
   buttons); "last sync: watch … / phone …".
2. **Trends** – 4, 12, 52, 104, 260 or 520 weeks (beyond a year aggregated per ISO week): one
   sparkline tile per metric of `series.METRICS` (latest value, delta against the 28-day mean,
   link), weight and VO2max only when the window holds values; the training hours per week and the
   acute:chronic ratio with the 0.8–1.3 band.
   **Metric pages** `/metrics/<key>` (since 2026-11-14, [analysis-concept.md](analysis-concept.md)):
   the full chart with the 28-day band and the 7-day mean, zoom, a second metric to compare
   (own chart, same days), the day's curve with the nights shaded, the weekday pattern and the
   daily table.
3. **Activities** – list with sport and name filter and hours per week by sport; detail with linked
   curves (HR, pace or speed, elevation, cadence, power), zone donut, lap chart, track without a map,
   laps, FIT download.
4. **Sleep** – nights as bed-to-wake bars; detail with the hypnogram, the night curves, stage shares
   against the 28-day mean, HRV, SpO2, watch vs strap.
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

## Parity with the TUI

Decided with the user on 2026-11-13: **every function of the TUI should be reachable in the
browser**, so nobody needs a terminal to run wristpull. The web layer stays a shell over the same
library functions (no SQL, no FIT parsing in `src/wristpull/web/`, boundary test). The TUI keeps
working as it is.

### The one structural problem: devices

The `web` container is reachable from the LAN and therefore gets no USB bus, no host network, no
system D-Bus (Bluetooth) and no adb keys (`docker-concept.md`). Importing from the watch, pulling the
phone, pulling the strap, pushing workouts and reading or cleaning the watch must run in another
process that the browser only *asks*.

**Decision: a device agent.** A third Compose service `agent` runs the same image with the rights
of `cli` (USB pass-through, host network, D-Bus socket, adb keys, the `wristpull-cli` AppArmor
profile) as a long-running loop (`wristpull agent`), without any published port. It starts with
`docker compose up -d` like `web` and `caddy` (`restart: unless-stopped`); when the AppArmor profile is
not installed only this service fails to start and the dashboard says "device agent offline".

Web and agent talk through the SQLite database they already share (WAL, exactly like `cli` and
`web` today):

- `agent_jobs` is the queue: the web app inserts a row (`kind`, JSON `params`, the equivalent
  command line), the agent claims the oldest queued row, runs it, writes log lines into
  `agent_job_log` and the result as JSON, and sets `done`, `error` or `stopped`. One job at a
  time. The browser polls the job partial every 2 s with htmx, as it does for coach runs.
- The job kinds are a **fixed list** with typed, validated parameters (`import_garmin`,
  `pull_phone`, `pull_strap`, `workout_push`, `workout_push_upcoming`, `workout_verify`,
  `watch_list`, `watch_remove`). No command lines, no paths from the browser; workout file names
  are checked like in the editor. The agent never executes anything else.
- **Stop:** the web app sets `stop_requested` on the running row; the agent hands the library the
  usual `stop=` callback (polled between files), so imports end after the current file.
- **Device state without MTP cost:** every second the agent writes a heartbeat and the USB
  presence it reads from sysfs (`idVendor` `091e` for the watch, an ADB interface for the phone)
  into `agent_state`. The dashboard shows "watch attached / phone attached / agent online" from
  that table and never probes the MTP tools itself.
- The agent reloads the config file for every job, so settings saved in the browser apply to the
  next job. With `[agent] import_on_plug = true` (default) it enqueues `import garmin` by itself once
  the watch has been on USB for ten seconds (the fenix enumerates twice), once per plug-in; this
  replaces the udev-started import unit. It also reports whether the host's Bluetooth adapter is
  rfkill-blocked (the device card shows it). Results are rendered with the same summary lines as in the TUI
  (`src/wristpull/jobtext.py`, Textual-free, shared by both shells).

Rejected: giving `web` the `cli` rights (a network-reachable process with the whole USB bus, the
host network and the system bus); mounting the Docker socket into `web` to run
`docker compose run cli` (root on the host); an HTTP or Unix-socket channel between the two
containers (a second channel with its own authentication, while the database is shared anyway).

Side by side: `cli`, the morning script and the udev import unit keep working. When one of them
opens the watch while an agent job uses it, the other side gets the known "device busy" message
(the same situation as the udev unit and the morning script today).

### Page by page

| TUI screen | Browser |
|---|---|
| 1 Overview | **Today** gains a "Devices" card (watch and phone attached, agent online, strap configured, the last device job) and a "Data store" card (raw files per source, errors), plus buttons for import, pull and the exports. |
| 2 Devices & Sync | **Sync** (new): the three forms (watch: dry run; phone: serial, trigger, sync, wait, dry run; strap: dry run, since days), live log, stop, result lines, the equivalent command line, the last 20 jobs. Runs on the agent. |
| 3 Days | **Days** (new): period presets, the daily table, a detail column per day (`detail.day_detail_rows`, moved out of the TUI screen so both shells share it). |
| 4 Activities | exists; adds the name filter, the archive path and a download link for the raw FIT. |
| 5 Sleep | exists; adds the source group (all / Garmin / strap). |
| 6 Files | **Files** (new): folder navigation under the data directory, archive row, FIT inspect, text preview, download, the host path to copy. `adb/` (private key) and `staging/` are never listed or served. |
| 7 Export & Report | **Export** (new): the same form, in-process jobs (`JobRegistry`), result with download links and a Markdown preview. |
| 8 Settings | **Settings** (new, 2026-11-13): the same field groups (no TUI theme), save = `validate_config` + `write_config`, the config file mounted read-write into `web` (same trust boundary as the data directory it already writes). *Revised with the user:* secrets are managed on the page too: password (start password `wristpull` until the first change, which the login forces), the band's auth key, the API keys and the Ollama address go into `<data_dir>/secrets.json` (mode 0600, `src/wristpull/secrets.py`), which overrides `.env`; `cli` and `agent` apply the store to their environment. Only what Compose needs before start (UID/GID, paths, `WRISTPULL_SITE`, the USB group) stays in `.env` and is shown read-only. "Check coach" as an in-process job is still open. |
| 9 Coach | exists; adds the dry run, tokens and cost per run, the profile editor (textarea) and the goal wizard as one form built from `coach.wizard.QUESTIONS`. |
| 0 Workouts (files) | exists; adds push (selected, upcoming, force, dry run), verify, delete with a confirmation step, the push history. Device parts run on the agent. |
| 0 Workouts (watch) | **Workouts on the watch** (new): list, re-read, remove with backup and confirmation. Runs on the agent. |
| ? Help | **Help** (new): pages, the CLI equivalent of every action, version and paths. |
| F12 | language switch in the navigation (cookie, read before `Accept-Language`). |

Not carried over: the command palette, the TUI theme list (the web has light and dark), the OSC 52
clipboard (a copy button in `app.js` instead), `import garmin --path <host folder>` (the container
cannot see host folders; an inbox under the data directory may follow later). Backup and restore
stay on the command line: an archive with the secrets is nothing to download in a browser.

### Security

Every new POST goes through the existing session and CSRF checks. Job kinds come from an
enumeration, parameters are typed (bool, int, allow lists), file names only through
`workouts.store.workout_file` or the files page's path check (resolved, inside the data directory,
no `..`). Destructive actions (delete a workout file, remove one from the watch) need a confirmation
step. Download routes serve only the data directory without `adb/`.

### Waves

- **W0** this section, CLAUDE.md.
- **W1** agent (`src/wristpull/agent/`, schema v7, `wristpull agent`, Compose service), `jobtext.py`,
  the Sync page, the device card on Today, the shared job partial.
- **W2** (done 2026-11-14 as wave P2 of the analysis concept) Days, Files, Export, the activity and sleep filters, the "More" sheet on the phone.
- **W3** Settings (config read-write in Compose), coach check, profile editor, wizard, dry run, language switch. *(done 2026-11-14: settings as of 2026-11-13, the coach pages and the language switch as wave P3 of the analysis concept)*
- **W4** workouts over the agent: push, verify, delete, the watch view. *(done 2026-11-14 as wave P4 of the analysis concept)*
- **W5** Help, navigation, screenshots, README table "TUI key ↔ web page".

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
