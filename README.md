<img src="docs/logos/wristpull-g-logo.svg" alt="wristpull" width="260">

# wristpull

Collects health and training data from wearables **without any vendor cloud**, stores the raw
files immutably, normalises everything into a local SQLite database and prepares it for analysis
with Claude (or a local LLM, dashboards, ...).

| Device | Path into wristpull | Status |
|---|---|---|
| Garmin fenix 6X Pro | USB (MTP), FIT files under `GARMIN/` | wave 1, done |
| Garmin fenix 6X Pro | Bluetooth via Gadgetbridge, FIT files pulled with ADB | wave 3, done |
| Amazfit Helio Strap | Gadgetbridge (Zepp OS), SQLite export pulled with ADB | wave 3, done |

The code repository never contains health data. Everything lands in `~/Health/wristpull/`
(configurable), the `.gitignore` additionally blocks `*.fit`, `*.sqlite`, `*.gpx` and data folders.

## Install

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
cd ~/dev/wristpull
uv sync
uv run wristpull config --init     # writes ~/.config/wristpull/config.toml
uv run wristpull status
uv sync --extra tui                # optional: the terminal UI (Textual)
uv run wristpull tui
uv sync --extra coach              # optional: Claude API backend for the training coach (anthropic SDK)
```

## Usage

Run every command as `uv run wristpull ...` from the repository, or install the script once with
`uv tool install --editable .` and call `wristpull ...` directly. The examples below use the short
form.

| Command | Purpose |
|---|---|
| `import garmin` | copy and parse new FIT files from the USB-connected watch |
| `pull phone` | trigger a Gadgetbridge export, pull database and FIT files over ADB, import them |
| `import gadgetbridge` | import an already copied Gadgetbridge export |
| `status` | what is stored, which device is connected |
| `inspect FILE` | message types and fields of a FIT file |
| `config` | show or create the configuration file |
| `export`, `report` | Markdown + CSV for Claude, weekly/monthly reports |
| `coach` | training coach: daily check, weekly review, questions, ratings via an LLM |
| `reparse` | re-parse archived raw files after a parser change |
| `tui` | the terminal UI over all of the above |

### Global options

| Option | Effect |
|---|---|
| `--config PATH` | use this config file instead of `~/.config/wristpull/config.toml` |
| `-q`, `--quiet` | print only the final summary line (no per-file output) |
| `--version` | print the version and exit |
| `-h`, `--help` | help for the program or any subcommand, e.g. `wristpull import garmin --help` |

Exit codes: `0` success, `1` no device found (watch or phone), `2` finished with file errors or
invalid arguments.

### Command reference

#### `wristpull import garmin`

Finds the watch, copies every FIT file that is not yet in the archive and parses it into the
database. Already archived files (same SHA-256) are skipped, so the command can run as often as
you like. After a successful import `daily_summary` is rebuilt.

| Option | Effect |
|---|---|
| `--path PATH` | use this `GARMIN` folder (or the mount point containing it) instead of auto-detection |
| `--dry-run` | list the files that would be imported, copy and parse nothing |

```sh
wristpull import garmin                      # auto-detect, mount via gio if needed
wristpull import garmin --dry-run            # what is new?
wristpull import garmin --path /run/user/1000/gvfs/mtp:host=091e_4cdb_<id>/Primary/GARMIN
wristpull import garmin --path ~/backup/fenix  # any directory tree with a GARMIN folder or FIT files
wristpull -q import garmin                   # summary line only, e.g. for cron/udev
```

Output per file: `+ Monitor/MAVG1415.FIT: heart_rate=672, stress=96, ...`, then a summary
`<root>: 37 FIT files, 37 new, 0 already archived, 0 errors` and the inserted row counts.
Files that cannot be decoded are still archived and marked `parse_status = 'error'` in `raw_files`.

#### `wristpull status`

Shows the config and database paths, the time of the last import, whether a watch is mounted (or
attached but not mounted), raw files per source and kind, and for every table the row count with
the first and last timestamp.

```sh
wristpull status
```

#### `wristpull inspect FILE`

Decodes one FIT file and prints its header (file type, creation time, device, serial), the
number of messages per message type, and for every field up to three example values. Fields and
messages that are not in the public FIT profile are shown as `unknown_<n>`, so new mappings can be
checked against the watch display.

| Option | Effect |
|---|---|
| `--messages NAMES` | comma-separated message names or numbers to print row by row, e.g. `stress_level,275` |
| `--limit N` | rows to print per selected message type (default 5) |

```sh
wristpull inspect ~/Health/wristpull/raw/garmin_usb/2026/monitor/<file>.FIT
wristpull inspect FILE.fit --messages stress_level --limit 20
wristpull inspect FILE.fit --messages session,lap,216
```

#### `wristpull config`

Without options prints the effective configuration in TOML form and says whether the file exists.

| Option | Effect |
|---|---|
| `--init` | write a default config file if none exists (never overwrites) |

```sh
wristpull config --init
wristpull config
wristpull --config ./other.toml config
```

#### `wristpull pull phone`

Talks to the phone over ADB: asks Gadgetbridge (Intent API) to sync its devices and export its
database, waits for the export file to change, pulls `Gadgetbridge.db` and every Garmin FIT file
that is not yet archived (files are hashed on the phone with `sha256sum`, so only new ones are
transferred), archives and parses everything, then rebuilds `daily_summary` for the sources
`gadgetbridge_zepp` and `gadgetbridge_garmin`.

| Option | Effect |
|---|---|
| `--serial SERIAL` | ADB serial (default: `[phone] serial` from the config, or the only connected device) |
| `--no-trigger` | do not send the sync/export intents, just pull what is there |
| `--no-sync` | send the export intent only |
| `--wait SECONDS` | how long to wait for the export to finish (default 60) |
| `--dry-run` | list what would be pulled, transfer nothing |

```sh
wristpull pull phone
wristpull pull phone --no-trigger          # Gadgetbridge already exported (auto export)
wristpull pull phone --dry-run
```

Requirements on the phone are in [docs/gadgetbridge.md](docs/gadgetbridge.md).

#### `wristpull import gadgetbridge`

Imports an export that was copied by other means (e.g. `adb pull` by hand, or a backup).

| Option | Effect |
|---|---|
| `--db FILE` | an exported `Gadgetbridge.db` |
| `--files DIR` | a local copy of `Android/data/nodomain.freeyourgadget.gadgetbridge/files` |

```sh
wristpull import gadgetbridge --db ~/tmp/Gadgetbridge.db --files ~/tmp/files
```

#### `wristpull export --for-claude --from DATE --to DATE`

Writes a compact Markdown summary (`summary.md`) plus four CSV attachments for a date range:
overview table, ISO-week aggregates, activity list with HR zones and training load, nights.
Garmin USB and Gadgetbridge data are merged (USB first), the Helio Strap appears as its own
columns. A month is a few KiB of Markdown, so it can be pasted into a Claude conversation; the
CSVs carry the per-day detail (`daily.csv`, `weekly.csv`, `activities.csv`, `sleep.csv`).

| Option | Effect |
|---|---|
| `--from DATE`, `--to DATE` | first and last local date (YYYY-MM-DD), inclusive |
| `--for-claude` | the compact Markdown + CSV format (currently the only one; the flag is accepted for clarity) |
| `--out DIR` | output directory (default `<data_dir>/exports/<from>_<to>`) |
| `--no-csv` | write only `summary.md` |
| `--stdout` | also print the Markdown |

```sh
wristpull export --for-claude --from 2026-10-01 --to 2026-10-31
wristpull export --from 2026-10-01 --to 2026-10-31 --stdout --no-csv | wl-copy   # straight to the clipboard
```

#### `wristpull report week|month`

Renders a readable Markdown report for the ISO week or calendar month containing `--date`
(default today): summary bullets with the change versus the previous period, optional SVG charts
(steps, resting HR Garmin vs Strap, sleep hours, stress and body battery), a per-day table,
activities and nights.

| Option | Effect |
|---|---|
| `--date DATE` | any date inside the week/month (default: today) |
| `--charts` | also write `steps.svg`, `resting_hr.svg`, `sleep.svg`, `stress.svg` next to the report |
| `--out DIR` | output directory (default `<data_dir>/reports/<period>-<label>`) |
| `--stdout` | also print the Markdown |

```sh
wristpull report week                       # current week
wristpull report week --date 2026-10-20 --charts
wristpull report month --date 2026-10-01 --stdout
```

Setting up Gadgetbridge on the phone (install, pairing both devices, the Helio Strap
authentication key, export and Intent API settings, ADB commands) is documented in
[docs/gadgetbridge.md](docs/gadgetbridge.md).

#### `wristpull coach`

The training coach sends an aggregated data package to an LLM and stores the answer. Backends:
**Ollama** (local, nothing leaves the machine), **Claude API** (official SDK, optional extra
`uv sync --extra coach`, key in `ANTHROPIC_API_KEY` or via `ant auth login`) and **OpenRouter**
(key in `OPENROUTER_API_KEY`). Concept: [docs/coach-concept.md](docs/coach-concept.md).

| Command | Effect |
|---|---|
| `coach daily` | morning check: last night, yesterday, 7-day table, baselines, training load, warning flags → status, a suggestion for today, things to watch, numbers used |
| `coach weekly` | review of the last completed week (the current one when run on a Sunday) vs the week before: activities, nights, 6-week trend, load → review, recovery, load, a plan for next week (Mon–Sun), questions, numbers used |
| `coach ask "…"` | a free question over the daily data package |
| `coach show [--last N] [--kind K]` | print stored answers with their rating |
| `coach rate RUN -1|0|1 [--note …]` | rate a run; the coach sees ratings and notes in later runs |
| `coach profile [--wizard]` | path of the goal profile and its unfilled placeholders (exit 2 while some are open); `--wizard` asks multiple-choice questions and writes the generated block into the profile, keeping your own notes below it |

Options for `daily`/`weekly`/`ask`: `--backend ollama|claude|openrouter`, `--model`, `--lang de|en`,
`--date YYYY-MM-DD` (pretend today), `--dry-run` (write the full prompt to `<data_dir>/coach/dryrun/`
instead of calling the model), `--force` (run although the profile still has placeholders).
`[coach] weekly_model` lets the weekly review use a larger model. The answer's "numbers used"
section is checked against the data package; numbers that do not occur there are reported as a
note so hallucinated values stand out. With Claude the request opts into the server-side refusal
fallback, so a safety-classifier decline is retried on a fallback model inside the same call.

The **goal profile** is a Markdown file at `<data_dir>/coach/profile.md`. Either answer the wizard
(`coach profile --wizard`, or `g` on the TUI coach screen): goals, event, time budget, experience,
preferences, constraints, planning style, tone and optional age/weight/max HR become a generated
block between markers, and everything you write below it (your notes) survives a re-run. Or edit the
file directly; the first run creates a template with `{{placeholders}}`. The text goes to the model
with every run. Trends, baselines, the acute:chronic
workload ratio and recovery flags are computed in Python before the model sees anything, and the
model is told to use only those numbers. Every run (prompts, answer, tokens, cost estimate) lands in
`coach_runs`; feedback goes to `coach_feedback` and is shown to the coach in later runs.

```sh
wristpull coach profile                          # where to edit the goal profile
wristpull coach profile --wizard --lang de       # answer the goal questions, generate the profile block
wristpull coach daily --dry-run                  # look at what would be sent
wristpull coach daily                            # gemma3:4b via Ollama by default
wristpull coach weekly --backend claude          # Sunday review with Claude (ANTHROPIC_API_KEY)
wristpull coach ask "Is a long run sensible today?" --lang de
wristpull coach rate 7 1 --note "did the easy run"
```

#### `wristpull reparse`

Re-parses files that are already in the archive, replacing the rows that came from them. Use it
after a parser change (new field mapping, new table) instead of re-importing from the device:
the raw files never change, only their interpretation. Daily summaries of the touched sources
are rebuilt. A file that fails to parse ends up with no rows and `parse_status = 'error'`.

| Option | Effect |
|---|---|
| `--id N` | one raw file by `raw_files.id` (repeatable) |
| `--source S` | every file of a source: `garmin_usb`, `gadgetbridge_garmin`, `gadgetbridge` (the database export) |
| `--kind K` | every file of a kind: `activity`, `monitor`, `sleep`, `metrics`, `hrv`, `database` |
| `--errors` | only files whose last parse failed |
| `--all` | everything |

```sh
wristpull reparse --source gadgetbridge        # strap tables after a Gadgetbridge parser change
wristpull reparse --kind sleep                 # all Garmin sleep files
wristpull reparse --errors                     # retry what failed last time
```

#### `wristpull tui`

A terminal UI over the same library functions (needs the optional extra: `uv sync --extra tui`).
It runs the commands above as background jobs with a live log, shows the data per day and browses
the data directory. A tab row under the header lists the nine screens with their digit keys and
marks the active one; tabs can also be clicked. German and English, switchable at runtime with `F12`; the language comes from
`[ui] language`, else from `LANGUAGE`/`LC_ALL`/`LANG`, else English.

| Key | Screen / action |
|---|---|
| `1` | Overview: device status, data store, 7-day KPIs with sparklines, recent activities, notes |
| `2` | Devices & Sync: forms for `import garmin` and `pull phone`, live log, last result, CLI equivalent (`c` copies it), `x` stops the running job after the current file |
| `3` | Days: `daily_summary` per day (Garmin merged + strap), `←`/`→` shift the period, `w` week, `m` month, `d` default range; detail pane for the highlighted day |
| `4` | Activities: list of the period with a text filter (`f`), detail with laps, time in HR zones and the HR curve; `o` opens the raw FIT file |
| `5` | Sleep: nights of every source with a stage bar, durations, score, HRV, SpO2, breathing and HR of the night; `g` cycles all / Garmin / strap |
| `6` | Files: tree of the data directory; `Enter` on a `.fit` runs `inspect`, Markdown/CSV preview, `o` opens with `xdg-open`, `y` copies the path |
| `7` | Export & Report: Claude export or weekly/monthly report for a period with presets, result file list and Markdown preview; `o` opens the folder, `y`/`c` copy path or CLI |
| `8` | Settings: edit every config value including `[ui]` and `[coach]`, `Ctrl+S` validates and writes `config.toml` (language switches immediately, `data_dir` after a restart); `Ctrl+T` checks whether the coach backend is reachable |
| `9` | Coach: `d` daily check, `w` weekly review, type a question and press Enter, `p` dry run; runs table with tokens, cost and rating, the answer beside it; `+` `−` `0` rate the selected run, `g` goal wizard (multiple choice → profile), `e` edit the profile in `$EDITOR` |
| `i` `p` `e` `r` | on the overview: import Garmin, pull phone, export the default range for Claude, weekly report with charts |
| `/` `?` `F12` `q` | command palette, help, language, quit |

Jobs run in a worker thread with their own SQLite connection; only one job runs at a time.

### Configuration file

`~/.config/wristpull/config.toml` (override with `--config`):

```toml
[storage]
data_dir = "~/Health/wristpull"          # raw files and database

[time]
timezone = "Europe/Berlin"               # IANA zone for local_date

[garmin]
folders = ["Activity", "Monitor", "Sleep", "Metrics", "HRVStatus"]
mount_hints = []                         # extra directories that contain GARMIN/

[phone]
serial = "<adb serial>"                   # ADB serial of the phone running Gadgetbridge
export_dir = "/sdcard/Gadgetbridge"

[ui]
language = "de"                          # TUI language: "de", "en" or "" for the system locale
theme = "textual-dark"                   # any Textual theme name
range_days = 28                          # default period of the data screens and the export

[coach]
backend = "ollama"                       # ollama | claude | openrouter
model = "gemma3:4b"
weekly_model = ""                        # optional larger model for the weekly review
api_key_env = ""                         # env var with the key; default ANTHROPIC_API_KEY / OPENROUTER_API_KEY
ollama_url = "http://localhost:11434"
num_ctx = 8192                           # Ollama context window
max_tokens = 3000                        # answer length cap (Claude-class models write ~1500–2500 tokens for a review)
timeout_s = 600
token_budget = 6000                      # size of the data package (12000 is fine for Claude)
effort = "medium"                        # Claude only
language = ""                            # answer language, "" = UI language
```

### Helper commands around the watch

These are system commands, not part of wristpull, but they are what you need when the watch does
not show up:

```sh
lsusb | grep -i garmin                  # is the watch attached? (ID 091e:4cdb)
gio mount -li | grep -A2 -i mtp         # MTP volumes and their activation_root URLs
gio mount mtp://091e_4cdb_<serial>/      # mount the watch via gvfs
gio mount -u mtp://091e_4cdb_<serial>/   # unmount (eject) before unplugging
ls /run/user/$(id -u)/gvfs/              # where gvfs mounted it
fuser -v /dev/bus/usb/<bus>/<dev>        # which process holds the USB device
kquitapp6 kiod6                          # KDE only: release the device so gio can open it
kioclient ls "mtp:/fenix 6X Pro/Primary/GARMIN/"                 # KDE alternative: list via KIO
kioclient copy "mtp:/fenix 6X Pro/Primary/GARMIN/Monitor" ~/tmp/ # KDE alternative: copy via KIO
```

### Looking at the data

The `sqlite3` command-line tool is not installed on every system; Python's built-in shell works the
same way (`uv run python -m sqlite3 DB "SQL"`, or interactive without the SQL argument).

```sh
uv run python -m sqlite3 ~/Health/wristpull/wristpull.sqlite        # interactive shell
uv run python -m sqlite3 ~/Health/wristpull/wristpull.sqlite \
  "SELECT local_date, sport, round(distance_m/1000,1) AS km, round(timer_s/60) AS min, avg_hr, training_load
   FROM activities ORDER BY start_utc DESC LIMIT 10"
uv run python -m sqlite3 ~/Health/wristpull/wristpull.sqlite \
  "SELECT local_date, steps, resting_hr, stress_avg, body_battery_min, body_battery_max, sleep_score
   FROM daily_summary ORDER BY local_date DESC LIMIT 14"
uv run python -m sqlite3 ~/Health/wristpull/wristpull.sqlite \
  "SELECT kind, parse_status, count(*) FROM raw_files GROUP BY 1, 2"
uv run python -m sqlite3 ~/Health/wristpull/wristpull.sqlite \
  "SELECT id, kind, created_at_utc, backend, model, input_tokens, output_tokens, cost_usd, status
   FROM coach_runs ORDER BY id DESC LIMIT 10"
```

The TUI shows the same data without SQL (`wristpull tui`: days, activities, sleep, files), and
`wristpull coach show --last 3` prints the latest coach answers.

### Connecting the fenix 6

The fenix 6 presents itself to Linux as an **MTP** device, not as a USB drive. gvfs mounts it at
`/run/user/<uid>/gvfs/mtp:host=091e_.../Primary/GARMIN`, which wristpull searches automatically
(along with `/run/media/<user>/*`, `/media/*`, `/mnt/*` and the `mount_hints` from the config).
If the watch is attached but not mounted, `wristpull import garmin` runs `gio mount` on every
Garmin MTP device that `gio mount -li` reports.

On **KDE** the `kiod6` service grabs the MTP device as soon as the watch is plugged in, and gvfs
then fails with "Unable to open MTP device". wristpull detects this and prints the workaround:

```sh
kquitapp6 kiod6 && gio mount mtp://091e_4cdb_<serial>/
wristpull import garmin
```

Alternatively copy the folders with KIO (`kioclient copy "mtp:/fenix 6X Pro/Primary/GARMIN/Monitor" ...`)
and import the copy with `--path`.

Files on the watch are only read, never deleted or modified.

Folder layout verified on a fenix 6X Pro (firmware 28.02): `Activity/` holds one
`YYYY-MM-DD-hh-mm-ss.fit` per workout, `Monitor/` the daily wellness files, `Sleep/`, `Metrics/`
and `HRVStatus/` (not `HRV`) the rest. The watch keeps wellness files only for a short time (on
first contact `Monitor/` held just the current day), so import regularly, ideally daily.

## Website and screenshots

`website/index.html` is a static one-page presentation of the project (reasons, pipeline, features,
LLM choice, screens, how to support it); `screenshots/` holds the pseudonymised TUI renders it uses,
produced from the synthetic test fixtures only. Open the page locally with
`python3 -m http.server 8000` from the repository root and visit `/website/`.

## Data layout

```
~/Health/wristpull/
├── wristpull.sqlite
├── raw/<source>/<year>/<kind>/<sha256-prefix>_<original name>
├── exports/<from>_<to>/            summary.md + CSVs (wristpull export)
├── reports/<week|month>-<label>/   report.md (+ SVG charts)
└── coach/
    ├── profile.md                  the goal profile sent to the coach (edit it)
    └── dryrun/<timestamp>_<kind>.md  full prompts written by --dry-run
```

`source` is `garmin_usb`, `gadgetbridge_garmin` or `gadgetbridge_zepp`; the exported Gadgetbridge
database itself is archived under `gadgetbridge/<year>/database/`. Every raw file is identified by
its SHA-256 (`raw_files` table), stored read-only and never overwritten; an import is idempotent and
can be re-run at any time. Coach answers are not files but rows in `coach_runs`
(`wristpull coach show`).

### Sources and how they relate

| source | device | what is read | notes |
|---|---|---|---|
| `garmin_usb` | fenix | FIT files from the watch over USB | complete, including activities with per-second records |
| `gadgetbridge_garmin` | fenix | the same FIT files, fetched by Gadgetbridge over Bluetooth and pulled from the phone | same parser; the watch splits them into smaller files |
| `gadgetbridge_zepp` | Helio Strap | tables of the exported Gadgetbridge database | per-minute samples, no raw files exist |

Two things seen on the first real night (2026-11-01) shape how the sources complement each other:

- The watch **removes monitoring files once Gadgetbridge has synced them**. After an overnight
  Bluetooth sync the USB `Monitor/` folder only holds what was recorded since; the night's
  wellness data then arrives through `pull phone` (`gadgetbridge_garmin`). Importing over USB
  before the phone syncs keeps both paths complete; otherwise the merge rule covers it.
- Sleep, HRV status and metrics files stay on the watch and come through both paths.
- The same applies to **any other client that syncs the watch**. On the first night the Garmin
  Connect app fetched the night's monitoring file first (Connect shows HR and SpO2 until 06:00),
  so neither Gadgetbridge nor USB ever saw those samples: wristpull has HR, stress, body battery
  and SpO2 only until 00:08 and again from 06:51 local. Whoever syncs first gets the file. For a
  cloud-free setup, unpair the Garmin Connect app (or disable its sync) and let Gadgetbridge
  and USB be the only readers.
- The Helio Strap's sleep comes from the band's own **sleep session reports**
  (`HUAMI_SLEEP_SESSION_SAMPLE`, stages light/deep/REM/awake with a score), the same data
  current Gadgetbridge code overlays on its charts. Gadgetbridge 0.95.1 still shows the older
  estimate from the per-minute flags (every minute the band flags as "sleep"), which came out
  about 40 minutes longer than the band's own session on the first night. The per-minute flags
  are only a fallback here, for databases without session reports, interpreted with
  Gadgetbridge's thresholds.
- Garmin's sleep start and end are the watch's **sleep window** (event 74 in the sleep file,
  bedtime to wake-up, what Garmin Connect shows as the sleep period). Minutes inside the window
  without a stage record are stored as `unmeasurable` stages so the gap stays visible.

`garmin_usb` and `gadgetbridge_garmin` describe the same watch, so their readings overlap. They
are kept apart in the sample tables and in `daily_summary` (one row per source and day). The
export and report layer merges them (`merge.py`): daily metrics are coalesced column by column,
USB first, Gadgetbridge fills gaps; activities and nights are de-duplicated by start minute. The
Helio Strap is a separate device and stays a separate source throughout (`strap_*` columns).

### Tables

All timestamps are ISO-8601 UTC strings (`2026-10-31T06:12:33Z`). Every sample row carries `source`,
`tz` (IANA zone from the config) and `local_date` (the calendar day in that zone).

| Table | Content (Garmin FIT message) |
|---|---|
| `raw_files` | archive index, parse status, FIT file type, device serial |
| `activities` | one row per session: sport, duration, distance, HR, speed, ascent, training effect, training load (`training_load_peak`), HR zone times as JSON |
| `activity_records` | per-second samples: HR, speed, cadence, altitude, position, power, temperature |
| `activity_laps` | laps |
| `heart_rate` | all-day heart rate (`monitoring.heart_rate`, 1-2 min resolution) |
| `resting_hr` | resting heart rate (`monitoring_hr_data`) |
| `steps` | cumulative daily counters per activity type (`monitoring.cycles`), with distance, active time, active calories |
| `intensity_minutes` | moderate / vigorous minutes |
| `stress` | stress level (`stress_level`), negative values kept as reported (-1 unmeasurable, -2 activity) |
| `body_battery` | body battery (`stress_level` field 3, undocumented, mapping from Gadgetbridge) |
| `sleep`, `sleep_stages` | one row per night from `sleep_level` stage changes plus `sleep_assessment` scores |
| `hrv`, `hrv_values` | HRV status summary and 5-minute RMSSD values (fenix 6 may not write these) |
| `spo2`, `respiration` | pulse ox and respiration rate |
| `temperature` | skin temperature (Helio Strap `GENERIC_TEMPERATURE_SAMPLE`) |
| `metrics` | sparse metrics: `vo2_max` (Garmin `max_met_data`), `pai_total` / `pai_today` (Helio Strap) |
| `daily_summary` | derived per day and source, rebuilt after every import |
| `coach_runs`, `coach_feedback`, `journal` | training-coach runs (prompts, answer, tokens, cost), the user's ratings, a daily note |

Verification status (fenix 6X Pro and Helio Strap, 2026-11-01): activity files (sessions, records
with GPS/altitude/HR, laps, HR zone times, training load) and the wellness fields steps, all-day
HR, stress, body battery, resting HR and respiration are confirmed against real data. After the
first real night the following were cross-checked row by row against Gadgetbridge's own tables
from the same export: Garmin SpO2 (28 samples, identical), Garmin HRV (91 five-minute RMSSD
values plus the nightly summary: weekly average, last night, 5-minute high, baseline, status),
strap HRV (400 values, identical), strap stress and resting HR (identical), strap sleep sessions
(stages and score as Gadgetbridge computes them). Garmin sleep stages are read from
`sleep_level` exactly as Gadgetbridge does; the night's stage sequence and the sleep assessment
scores (overall, quality, recovery, awakenings, average stress) were confirmed against the FIT
file; the sleep start (23:10) and HRV (40 ms) matched Garmin Connect. Still open: the Helio
Strap's SpO2 table (no samples yet) and Garmin SpO2 for the whole night, which only reaches
wristpull when no other app syncs the watch first (see "Sources and how they relate").

Conventions worth knowing:

- Step counters are cumulative per day and activity type; `daily_summary.steps` is the sum of the
  per-type maxima. A counter stamped exactly at local midnight is attributed to the day that ended.
- A sleep session starts with the first `sleep_level` record and ends with the last one (normally
  an `awake` record); `duration_s` excludes awake time. Records more than 8 h apart form separate
  sessions. Files with only awake/unmeasurable stages yield no sleep row.
- `heart_rate` holds wellness HR only; HR during workouts lives in `activity_records`.
- Helio Strap steps arrive per minute; wristpull stores them as a cumulative per-day counter with
  `activity_type = 'all'` so `steps` has the same meaning for every source (rows are replaced on
  re-import because the running sum can change when Gadgetbridge back-fills minutes).
- Helio Strap sleep is derived from the per-minute sleep/deep/REM flags: consecutive flagged
  minutes (gaps up to 30 min count as awake) form a session, sessions under 20 minutes are dropped.
  Garmin sleep comes from the watch's own stage records.
- Garmin rows inside the Gadgetbridge database are ignored on purpose; the FIT files on the phone
  are the richer source and go through the same parser as the USB import.
- Undocumented fields are not dropped silently: `wristpull inspect` lists them as `unknown_<n>`
  with sample values, so new mappings can be verified against the watch display.

## Development

```sh
uv sync --extra tui --extra coach        # create .venv and install dependencies incl. TUI and Claude SDK
uv run pytest -q                         # run the test suite (TUI tests skip without the extra)
uv run pytest -q tests/test_fit_garmin.py -k sleep   # a subset
uv run pyflakes src tests                # lint
uv run wristpull --help                  # run the CLI from the checkout
uv tool install --editable .             # optional: `wristpull` on PATH, follows the checkout
uv lock --upgrade                        # refresh uv.lock
uv run pybabel extract -F babel.cfg -o src/wristpull/tui/locale/wristpull.pot --no-location src/wristpull/tui
uv run pybabel update -i src/wristpull/tui/locale/wristpull.pot -d src/wristpull/tui/locale -D wristpull
uv run pybabel compile -d src/wristpull/tui/locale -D wristpull   # after editing de/LC_MESSAGES/wristpull.po
```

The TUI (`src/wristpull/tui/`) only calls library functions: no SQL and no FIT parsing in there
(`tests/test_tui_boundary.py` enforces it). Strings are wrapped in `_()` at render time; a test
fails when the German catalog misses a string or the compiled `.mo` is stale.

The coach (`src/wristpull/coach/`) keeps SQL in `store.py` and `context.py`; everything the model
sees is built from the aggregate functions, and all tests talk to a `FakeBackend` (no network). The
Claude backend is exercised against a fake client, Ollama and OpenRouter against a fake `urlopen`.

Tests use **synthetic** FIT files generated by `tests/fitwriter.py` (a minimal FIT encoder), so no
real health data is needed or allowed in the repository. Code, comments and commits are in English.

## Roadmap

1. ~~Garmin USB import, FIT to SQLite (activities, monitoring, sleep), `status`~~
2. ~~`pull phone`: Gadgetbridge database and FIT files via ADB, Helio Strap parsing~~ (done before wave 2 because the devices were set up first)
3. ~~`export --for-claude --from --to` (compact Markdown + CSV) and `report week|month`, merging the Garmin sources~~
4. ~~Verify sleep/HRV/SpO2 mappings against a real night~~ (2026-11-01, see verification status) — next: udev/systemd trigger for automatic imports
5. ~~Terminal UI (`wristpull tui`): overview, sync jobs with cancel, days, activities, sleep, files, export & report, editable settings; German/English~~ — concept in [docs/tui-concept.md](docs/tui-concept.md), mockups in [mockups/](mockups/README.md)
6. Training coach via LLM: ~~C1 daily check and questions with Ollama, goal profile, local memory~~ ~~C2 weekly review, Claude API and OpenRouter backends, number check, ratings~~ ~~C3 goal wizard, TUI coach screen~~ — next: journal, systemd timer, plan adherence, ICS export; concept in [docs/coach-concept.md](docs/coach-concept.md)
7. Later: MCP server / Claude skill over the SQLite, dashboards, nutrition source (MyNetDiary or own logging)
