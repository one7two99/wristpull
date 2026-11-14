<img src="docs/logos/wristpull-g-logo.svg" alt="wristpull" width="260">

Project page: <https://one7two99.github.io/wristpull/>

[![CI](https://github.com/one7two99/wristpull/actions/workflows/ci.yml/badge.svg)](https://github.com/one7two99/wristpull/actions/workflows/ci.yml) tests on Linux, macOS and Windows (hardware-free; see [docs/platforms.md](docs/platforms.md))

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

The primary way is the Docker stack on Linux: one image runs the web dashboard and every command,
including the watch over USB, the phone over ADB and the strap over Bluetooth, so the host needs
neither Python nor uv. On a fresh machine `contrib/bin/wristpull-setup` walks through the whole
setup and asks only for what is missing (or restores a backup); [docs/setup.md](docs/setup.md) is
the same checklist by hand, the details are under "Running everything in Docker".

```sh
git clone https://github.com/one7two99/wristpull.git ~/dev/wristpull && cd ~/dev/wristpull
contrib/bin/wristpull-setup        # Docker, .env, image, dashboard, keys, udev rule, AppArmor profile
contrib/bin/wristpull status       # every command through the container
```

The native installation (Python 3.14 and [uv](https://docs.astral.sh/uv/)) runs the same package
with the same commands. It is the way for development, and for macOS and Windows, where Docker
cannot reach the devices. [docs/platforms.md](docs/platforms.md) says which way fits which machine
(Linux, macOS, Windows, Android, iPhone), what each path imports and what it needs.

```sh
cd ~/dev/wristpull
uv sync --extra tui --extra coach --extra web --extra ble   # terminal UI, Claude backend, web dashboard, strap over Bluetooth
uv run wristpull config --init     # writes ~/.config/wristpull/config.toml
uv run wristpull status
```

## Usage

With the Docker stack every command runs as `docker compose run --rm cli wristpull ...` from the
checkout, or shorter through the wrapper `contrib/bin/wristpull ...`; in a native installation it
is `uv run wristpull ...` (or once `uv tool install --editable .`, then `wristpull ...`). The
examples below use the short form.

| Command | Purpose |
|---|---|
| `import garmin` | copy and parse new FIT files from the USB-connected watch |
| `pull phone` | trigger a Gadgetbridge export, pull database and FIT files over ADB, import them |
| `pull strap` | read the Helio Strap directly over Bluetooth (no phone, no Gadgetbridge) and import it |
| `strap scan\|pair\|info` | find the band, store its auth key, show where every fetch kind left off |
| `import gadgetbridge` | import an already copied Gadgetbridge export |
| `import garmin-export` | import the Garmin Connect account export (your whole history as FIT files) |
| `status` | what is stored, which device is connected |
| `inspect FILE` | message types and fields of a FIT file |
| `config` | show or create the configuration file |
| `export`, `report` | Markdown + CSV for Claude, weekly/monthly reports |
| `coach` | training coach: daily check, weekly review, questions, ratings via an LLM |
| `workout` | structured workouts as Markdown, pushed to the watch as FIT files |
| `reparse` | re-parse archived raw files after a parser change |
| `backup create\|show\|restore` | the whole state (data, config, `.env`, certificates) as one tar.gz, and back onto a new machine; the same from the dashboard's Backup page |
| `tui` | the terminal UI over all of the above |
| `serve` | the web dashboard for phone and browser (also as a Docker container) |

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

Reads the watch over USB with the MTP tools, copies every FIT file that is not yet in the archive
and parses it into the database. Already archived files (same SHA-256) are skipped, so the command
can run as often as you like; files the archive already knows by name and size are not even
downloaded. After a successful import `daily_summary` is rebuilt.

| Option | Effect |
|---|---|
| `--path PATH` | import this `GARMIN` folder (or a directory containing one) instead of the watch: a copy made with a file manager, or a mass-storage mount |
| `--dry-run` | list the files that would be imported, copy and parse nothing |

```sh
wristpull import garmin                      # the watch on the cable
wristpull import garmin --dry-run            # what is new?
wristpull import garmin --path ~/backup/fenix  # any directory tree with a GARMIN folder or FIT files
wristpull -q import garmin                   # summary line only, e.g. for the udev-started import
```

Output per file: `+ Monitor/MAVG1415.FIT: heart_rate=672, stress=96, ...`, then a summary
`<root>: 37 FIT files, 37 new, 0 already archived, 0 errors` and the inserted row counts.
Files that cannot be decoded are still archived and marked `parse_status = 'error'` in `raw_files`.

#### `wristpull status`

Shows the config and database paths, the time of the last import, whether a watch answers over
USB (and why not), the strap's address and key state, raw files per source and kind, and for every
table the row count with the first and last timestamp.

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

#### `wristpull pull strap`

Talks to the Amazfit Helio Strap (any Zepp OS band) over Bluetooth Low Energy, the way Gadgetbridge
does, so the phone is not needed at all ([concept](docs/strap-ble-concept.md)). It needs the `ble`
extra (`uv sync --extra ble`; the Docker image has it), the band's 16-byte auth key and the band in
range and not connected to the phone (a band holds one connection; disconnect it in Gadgetbridge or
remove it there).

Per fetch kind (activity minutes, stress, SpO2, HRV, resting heart rate, respiration, the band's
sleep sessions, PAI, the day's maximum heart rate, and workouts) the band is asked for everything
since the last sample wristpull stored (`strap_sync`, first run: `[strap] since_days`). Workouts come
one per request: the summary (a protobuf message, up to ten per pull), then its details track
(one point per second with heart rate, pace, distance, GPS where the band has it); they become
`activities` with `activity_records` and `activity_laps`, the band's VO2max estimate a `vo2_max`
metric. The Helio Strap reports automatically recognised workouts as sport `auto_detected`
(`sub_sport` says so); PAI is stored as the metrics `pai_total` and `pai_today`, the maximum heart
rate as `hr_max`.
Each answer is archived as an immutable buffer file under `raw/zepp_ble/`, parsed into the sample
tables as source `zepp_ble` and the daily summaries are rebuilt. The data stays on the band
(`keep_on_band`); a second run only brings what is new. Activity is always asked from the local
midnight of the last synced day because its step counts are cumulative per day.

| Option | Effect |
|---|---|
| `--since-days N` | how far back the first pull asks (default `[strap] since_days`, 30) |
| `--delete` | let the band delete what it sent (default: keep) |
| `--dry-run` | ask the band how much it holds per kind, transfer nothing |

```sh
wristpull strap scan                                   # bands in range, strongest first
wristpull strap pair --authkey 0x<32 hex> --address AA:BB:CC:DD:EE:02   # checks the key against the band, writes the address
wristpull pull strap
wristpull strap info                                   # address, where the key comes from, last sample per kind
```

The auth key is the one Gadgetbridge uses (device settings → *Auth key*; it is also in a Gadgetbridge
backup under `preferences/device_<MAC>.json`) or comes from the Zepp account with `huami-token`. Like
the coach API keys it lives in the environment, never in the config file: `WRISTPULL_STRAP_AUTHKEY`
in `.env` for Docker (`strap pair` prints the line), or exported in the shell of a native install.

When `strap scan` sees the band but `pull strap` reports it as not found or times out, the host's
adapter is usually stuck in a scan that nothing can stop (`bluetoothctl show` says `Discovering: yes`
although no scanner is running). `pull strap` now notices that state when its scan comes back empty
and power-cycles the adapter itself over D-Bus before it looks again (the log says so); by hand it is
`bluetoothctl power off && bluetoothctl power on`, and the morning script says so when it sees that state.

#### `wristpull import gadgetbridge`

Imports an export that was copied by other means (e.g. `adb pull` by hand, or a backup).

| Option | Effect |
|---|---|
| `--db FILE` | an exported `Gadgetbridge.db` |
| `--files DIR` | a local copy of `Android/data/nodomain.freeyourgadget.gadgetbridge/files` |

```sh
wristpull import gadgetbridge --db ~/tmp/Gadgetbridge.db --files ~/tmp/files
```

#### `wristpull import garmin-export`

Imports the account export you can request at Garmin Connect (Account → Data Management →
Export your data; Garmin mails a link to a zip, sometimes split into `…_1.zip`, `…_2.zip`). Pass
the zip as downloaded, the inner `UploadedFiles_*.zip`, or an extracted directory:

```sh
uv run wristpull import garmin-export ~/Downloads/Garmin-Export/<id>_1.zip --dry-run
uv run wristpull import garmin-export ~/Downloads/Garmin-Export/<id>_1.zip
# in Docker: put the zip under <data_dir>/imports/ (mounted at /data) or mount the folder
docker compose run --rm -v ~/Downloads/Garmin-Export:/export:ro cli wristpull import garmin-export /export/<id>_1.zip
```

What it does:

- Reads `DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_*.zip` in place, file by file, nothing
  is extracted to disk. Every FIT file the account ever uploaded goes through the normal parser
  under the source **`garmin_connect`**: monitoring, sleep, HRV status, metrics, activities.
- The `raw_files.kind` comes from the FIT file type (`monitoring_b` → monitor, 49 → sleep,
  68 → hrv, 44 → metrics, `activity`; anything else → other), the file name loses the e-mail
  prefix Connect puts in front (`<account>_7102332115.fit` → `7102332115.fit`).
- Files already archived from the watch or the phone are byte-identical and skipped by the
  SHA-256 check; a second run imports nothing. Parse errors are archived with
  `parse_status = 'error'` and listed at the end (`reparse --source garmin_connect --errors`).
- Expect about 25 minutes for a ten-year export (measured: 15,470 files in 23 minutes on a
  laptop, 303 of them duplicates inside the export, one truncated file); a progress line appears
  every 500 files, one commit per file so an interrupted run can simply be repeated, and a second
  run finishes in seconds.
- The JSON files come along: the **daily summaries** (`DI-Connect-Aggregator/UDSFile_*.json`,
  kind `uds`) go into their own table `connect_daily` (steps, distance, calories, intensity
  minutes, HR min/max, resting HR, stress, body battery, SpO2, respiration per day) because they
  are aggregates, not samples; `daily_summary` takes them only for columns the samples left
  empty. The **nights** (`DI-Connect-Wellness/*_sleepData.json`, kind `sleep_json`) become
  `sleep` rows with stage totals, sleep score, quality/recovery score and awakenings; the export
  has no stage timeline, so no `sleep_stages`; a night the FIT files already delivered keeps the
  FIT version. Placeholder records (the FIT epoch 1989-12-31, empty days) are discarded and
  counted. Later date blocks win on overlapping days.
- **Activity names and VO2max:** `summarizedActivities.json` carries the names you gave your
  activities in Connect; an archived activity of any source that starts within two minutes with a
  compatible sport gets that name (`activities.name`, shown before the profile name in TUI,
  dashboard and export; never overwritten). `MetricsMaxMetData_*.json` fills the VO2max history
  (`metrics`, `vo2_max`) for days no FIT file covers, `userBioMetrics.json` the **weight history**
  (`metrics`, `weight_kg`, the export's grams as kilograms, last entry per day; the static profile
  weight without a date is ignored). The dashboard's Trends page shows weight and VO2max as point
  series whenever the window holds values.
- Local dates are the export's `calendarDate`; the `tz` column carries your configured zone, past
  time zones are not reconstructed. Hydration, nutrition, gear, training plans and the other
  JSON files are not imported.
- The merge keeps the watch and the phone first: the export only fills days and nights they do
  not have (`merge.GARMIN_PRIORITY`). `reparse --source garmin_connect --kind uds` (or
  `sleep_json`, `activities_json`, `maxmet`, `biometrics`) re-parses the JSON files after a parser change.

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
| `coach insights [--weeks N]` | the insights report: readiness with its components, sleep regularity, lagged correlations with sweet spots and tag effects (all computed in Python, see the Insights page) → what the data shows, what it means, one thing to try, numbers used; model `[coach] insights_model`, else the weekly one |
| `coach tag DATE +tag -tag` | set or clear day tags for the insights (`alcohol`, `alcohol_free`, `caffeine_late`, `late_meal`, `sick`, `travel`, `sauna`, `nap`, `stress_high`, `rest_day`, `race`); `today` works as the date |
| `coach note "…" [--date D]` | one journal line per day (how you felt, what you did off the record); the last 7 days go into every data package |
| `coach notes [--last N]` | print recent journal lines |
| `coach plan-ics [--out F] [--hour H]` | the last weekly plan as an iCalendar file: timed events for sessions, all-day notes for rest days |
| `coach profile [--wizard]` | path of the goal profile and its unfilled placeholders (exit 2 while some are open); `--wizard` asks multiple-choice questions and writes the generated block into the profile, keeping your own notes below it |

#### `wristpull workout …` — structured workouts onto the watch, without Garmin Connect

A workout is one Markdown file under `<data_dir>/workouts/`, written by hand, from a template or
drafted by the coach from its weekly plan. `workout push` encodes it as a FIT workout file and
copies it to `GARMIN/NewFiles/` on the watch over USB; the watch imports it when the cable is
unplugged and lists it under *Training → Workouts*, with the heart-rate targets guiding each step.
Confirmed on a fenix 6X Pro (firmware 28.02) over MTP on 2026-11-02: the file appears under the Run
activity's workouts right after unplugging, no restart, no eject needed.

```markdown
---
date: 2026-11-05
sport: running
name: Easy run
status: draft
---
# Easy run
- warmup 10 min hr <= 130
- run 40 min hr 135-150
- cooldown 5 min hr <= 125
```

Each step is `- <intensity> <length> [hr <target>] [# note]`. Intensities: `warmup`, `run`,
`recovery`, `cooldown`, `rest` (German words work too). Lengths: `40 min`, `1:15 h`, `5000 m`,
`5 km`, `open` (until the lap key). Heart-rate targets: a bpm range `135-150`, a cap `<= 130`, a floor
`>= 120`, a Garmin zone `Z2`, or percent of max HR `70-80 %`. `sport` is `running` (default),
`cycling`, `walking`, `strength` or `cardio`. `status` is maintained by wristpull:
`draft` → `pushed` → `verified` (seen under `GARMIN/Workouts/`).

**Interval blocks** are a `- repeat 6x` line (also `- 6x` or `- wiederhole 6x`) with the steps to
repeat indented below it; the block ends at the next step that is not indented. The count is the
number of repetitions in all (2–99); blocks cannot be nested. The coach does not draft intervals;
write them by hand, or start from `workout new --template intervals` (`t` on TUI screen 0 cycles
time → distance → intervals).

```markdown
- warmup 10 min hr <= 130
- repeat 6x
  - run 400 m hr Z5
  - recovery 2 min hr <= 130
- cooldown 5 min hr <= 125
```

In the FIT file the block is written the way Garmin Connect writes it: the block's steps, then a
repeat step pointing back at the first of them (`duration_type` 6, `repeat_steps` = count); the
header total counts every repetition.

The FIT files follow what Garmin Connect itself writes onto the watch (product `connect`, a
`file_creator` record, `capabilities` tcx; checked against the Connect-made files under
`GARMIN/Workouts/` of a fenix 6X Pro). Drafts from the plan
take the text before the first colon or comma as the name, keep the full session text as the
description, read the target from the intensity column first (`Z2`, `zone 2`, `150-160`, words
like easy/tempo/hard) and pick the sport from the words (strength, ergometer, walk, …).

| Command | What it does |
|---|---|
| `workout new [--date D] [--name N] [--template time\|distance\|intervals] [--minutes M] [--metres M] [--edit]` | write a file from the template (warm-up, main part by time or distance or a 6 × 400 m interval block, cool-down) and print its path; `--edit` opens `$EDITOR` |
| `workout list [--from D --to D]` | date, name, steps, total, status and validation errors per file |
| `workout check FILE…` | parse and validate, print the steps the way the watch will show them (exit 2 on errors) |
| `workout push [FILE…] [--date D] [--upcoming] [--force] [--path P] [--folder NewFiles\|Workouts] [--schedule] [--dry-run]` | build the FIT files and copy them to the watch (`--dry-run`: to `<data_dir>/workouts/out/`); `--upcoming` takes the workouts from today on that are not on the watch (never pushed, or removed again); `--force` also takes those already pushed and writes a file again even if an identical one is there; `--schedule` adds a schedule file with the planned dates (experimental); existing files on the watch are never overwritten |
| `workout verify [--path P]` | read `GARMIN/Workouts/` and mark pushed workouts the watch has taken over as `verified` |
| `workout watch [--path P]` | list the workouts on the watch: `active` ones in `GARMIN/Workouts/` and `pending` ones still in `GARMIN/NewFiles/` (active after unplugging), with sport, steps, created, origin (`wristpull` or `other`), the count against the limit (both together) and a warning when two share a name |
| `workout remove NAME… [--path P] [--yes]` | remove workouts from the watch by name or file name, active or pending (taking a push back before unplugging); each file is first copied to `<data_dir>/workouts/watch-backup/`; asks per file unless `--yes` |
| `workout from-plan [--week D]` | one draft per session of the coach's last weekly plan (rest days and days that already have a file are skipped) |

**The watch has a workout limit.** Garmin states 25 stored workouts for the fenix 6 series (users
report hitting it at about 20); the preloaded strength and yoga workouts count towards it. When the
limit is reached the watch shows "Exceeded maximum number of workouts" briefly on connecting and
silently deletes every new file from `NewFiles/` and `Workouts/` without importing it, which looks
exactly like a broken file. `workout watch` (or `w` on TUI screen 0) shows the count; remove old
workouts with `workout remove` before pushing more. Scheduled workouts (calendar) are
reported not to count towards the limit; wristpull's `--schedule` support is still being verified.

**Names must be unique within a week.** The watch keeps only one of two workouts with the same
name, so generated names end with the day (`Outdoor run 06.11.`, both from `from-plan` and from
`workout new` without `--name`). Two files of the same ISO week that share a name are flagged in
`workout list`/`check` and on TUI screen 0, and `push` refuses them until one is renamed (`name:` in
the front matter).

Every push is recorded in `workout_pushes` (name, date, SHA-256 of the FIT bytes, target, watch
serial, status). The `[workout]` section sets the shape of generated workouts (warm-up and cool-down
minutes and HR caps, default target of the main part) and the target folder.

Options for `daily`/`weekly`/`ask`: `--backend ollama|claude|openrouter`, `--model`, `--lang de|en`,
`--date YYYY-MM-DD` (pretend today), `--dry-run` (write the full prompt to `<data_dir>/coach/dryrun/`
instead of calling the model), `--force` (run although the profile still has placeholders).
`[coach] weekly_model` lets the weekly review use a larger model. The weekly review also compares the
plan the coach made the week before with the activities that actually happened (plan adherence) and
shows the week's journal lines. The answer's "numbers used"
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
| `--kind K` | every file of a kind: `activity`, `monitor`, `sleep`, `metrics`, `hrv`, `database`, or a Connect export JSON kind (`uds`, `sleep_json`, `activities_json`, `maxmet`, `biometrics`) |
| `--errors` | only files whose last parse failed |
| `--all` | everything |

```sh
wristpull reparse --source gadgetbridge        # strap tables after a Gadgetbridge parser change
wristpull reparse --kind sleep                 # all Garmin sleep files
wristpull reparse --errors                     # retry what failed last time
```

#### `wristpull backup create|show|restore`

Everything wristpull knows is the data directory, the config file, the `.env` next to
`compose.yaml` and the own-CA certificate files in `deploy/certs/`. `backup create` packs the four
into one `wristpull-backup-<date>.tar.gz` (`MANIFEST.json`, `env`, `config.toml`, `certs/…`,
`data/…`, `SHA256SUMS`; archive format 2, format 1 archives without `certs/` are still read):
`staging/` and `backups/` are left out, the database goes in as a consistent copy made with SQLite's
backup API, so the dashboard may keep running. The archive contains the secrets and is written with
mode 0600; keep it on a disk you trust. The default target is `<data_dir>/backups/`, the folder the
dashboard's Backup page uses as well. `backup show` prints the manifest. `backup restore` unpacks
into the data directory, writes the config, `.env` and the certificate files, and rewrites what
belongs to the machine: `UID`/`GID`, `WRISTPULL_DATA` and `WRISTPULL_CONFIG` in `.env`,
`[storage] data_dir` in the config. Secrets, timezone, strap address, raw-file modes and everything
else are kept; checksums are verified; nothing is deleted (`--force` lets the archive overwrite
existing files). `.env` and the config are rewritten in place (they are single-file bind mounts in
the containers), and an existing database is overwritten through the backup API instead of being
replaced, so the dashboard's and the agent's open connections keep working on the same file.

| Option | Effect |
|---|---|
| `create --out PATH` | directory (default `<data_dir>/backups/`) or a file name ending in `.tar.gz` |
| `create --env FILE` | the `.env` to include (default `WRISTPULL_ENV_FILE`, else `./.env` when `./compose.yaml` exists; otherwise the archive has no secrets) |
| `create --certs DIR` | the certificate folder to include (default `WRISTPULL_CERTS_DIR`, else `./deploy/certs` in a checkout) |
| `restore --data-dir DIR` | where the data goes (default: the configured data directory) |
| `restore --env FILE`, `--certs DIR` | where `.env` and the certificate files go (defaults as above) |
| `restore --host-data-dir`, `--host-config` | the paths as the host sees them, for runs inside the container |
| `restore --uid`, `--gid` | for `.env` (default: the current user) |
| `restore --force` | overwrite existing files |

```sh
wristpull backup create                                        # native install, from the checkout → <data_dir>/backups/
contrib/bin/wristpull-backup                                   # Docker: same, via the cli service
contrib/bin/wristpull-restore ~/Health/wristpull/backups/wristpull-backup-2026-11-08-0905.tar.gz
                                                               # new machine: clone, then this (builds the image, writes .env)
```

Moving to another machine is therefore: `wristpull-backup` here (or the Backup page in the browser),
copy the archive, `git clone` and `wristpull-restore` there, then `docker compose build && docker
compose up -d` and steps 4 to 6 of [docs/setup.md](docs/setup.md) (udev rule, AppArmor profile; the
phone needs Caddy's new certificate only when no own certificates were in the archive). The way
without the restore script: `docker compose up -d` on a fresh `.env`, log in with the start password,
"Restore a backup instead" and upload the archive on the Backup page (see "Web dashboard").

#### `wristpull tui`

A terminal UI over the same library functions (needs the optional extra: `uv sync --extra tui`).
It runs the commands above as background jobs with a live log, shows the data per day and browses
the data directory. A tab row under the header lists the ten screens with their digit keys and
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
| `9` | Coach: `d` daily check, `w` weekly review, type a question and press Enter, `p` dry run; runs table with tokens, cost and rating, the answer beside it; `+` `−` `0` rate the selected run, `g` goal wizard (multiple choice → profile), `e` edit the profile in `$EDITOR`, `t` draft workouts from the last weekly plan |
| `0` | Workouts: files of the next three weeks (and the last one) with status, preview of the steps as the watch shows them plus the Markdown; `n` new from the template (then `$EDITOR`), `t` template time/distance, `e` edit, `c` drafts from the coach plan, `p` push the selected one, `P` push every upcoming one not on the watch, `f` force push every upcoming one, `d` dry run, `v` verify against `GARMIN/Workouts/`, `Del` twice deletes the file, `←`/`→` shift the period. `w` switches to the workouts **on the watch**: active ones and those still waiting in `NewFiles` for the cable to be pulled, count against the limit, origin, steps preview, ⚠ for duplicate names; there `Del` twice removes one (active or waiting) after a backup, `r` reads the watch again |
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
folders = ["Activity", "Monitor", "Sleep", "Metrics", "HRVStatus"]   # read over USB (MTP), or from --path

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

[workout]
warmup_min = 10                          # shape of generated workouts (templates, drafts from the plan)
cooldown_min = 5
warmup_hr_max = 130
cooldown_hr_max = 125
default_target = "Z2"                    # main-part target when the plan names none: Z2, 135-150 or 70-80 %
schedule = false                         # also write a schedule file with the planned dates (experimental)
folder = "NewFiles"                      # NewFiles (imported when unplugged) or Workouts
```

### Helper commands around the watch

These are system commands, not part of wristpull, but they are what you need when the watch does
not show up:

```sh
lsusb | grep -i garmin                  # is the watch attached? (ID 091e:4cdb)
mtp-detect                              # does libmtp see it? (inside the container: contrib/bin/wristpull … or docker compose run --rm cli mtp-detect)
fuser -v /dev/bus/usb/<bus>/<dev>       # which process holds the USB device
udevadm info -q property /dev/bus/usb/<bus>/<dev> | grep -E 'ID_MTP|SYSTEMD'   # is the udev rule in effect?
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

The fenix 6 presents itself as an **MTP** device, not as a USB drive. wristpull reads it with the
libmtp command-line tools (`mtp-files`, `mtp-getfile`, …; in the Docker image, or `apt install
mtp-tools` natively), so no mount is involved, but **nothing else may hold the device**: a desktop
that mounts the watch (gvfs, KDE's `kiod6`) makes the tools fail with "device is busy", and
wristpull then says so (`holds the watch`). The udev rule `contrib/udev/91-wristpull-garmin-docker.rules`
keeps the desktop away from the watch for good; without it, unmount the watch in the file manager
first (on KDE `kquitapp6 kiod6`). Every call opens the device again (about six seconds on a fenix
6X Pro), which is fine for a daily import: the listing happens once, only unknown files are fetched.

Without a cable to this machine, copy the `GARMIN/` folder with any file manager and import the copy
with `--path`; the same works for a watch that appears as a USB drive.

Files on the watch are only read, never deleted or modified, with two deliberate exceptions for
workouts: `wristpull workout push` writes new FIT files into `GARMIN/NewFiles/` (or `Workouts/`),
never over an existing one, and `wristpull workout remove` (or `Del` in the TUI's watch view)
deletes a workout file directly in `GARMIN/Workouts/` or `GARMIN/NewFiles/` on request, after copying it to
`workouts/watch-backup/`. Sub-folders and every data folder stay untouched. The fenix 6X Pro imports them from `NewFiles` when the cable is pulled
(confirmed 2026-11-02); `workout verify` reports what the watch took and warns near the workout
limit. Whether a schedule file places a workout on the calendar date is still open.

Folder layout verified on a fenix 6X Pro (firmware 28.02): `Activity/` holds one
`YYYY-MM-DD-hh-mm-ss.fit` per workout, `Monitor/` the daily wellness files, `Sleep/`, `Metrics/`
and `HRVStatus/` (not `HRV`) the rest. The watch keeps wellness files only for a short time (on
first contact `Monitor/` held just the current day), so import regularly, ideally daily.

## Web dashboard

A small web app for the phone and the browser: **Today** (resting HR, HRV, sleep, stress, steps
and body battery against your 28-day mean, each with a two-week sparkline, today's Body Battery and
stress curve, warning signs, training load, the latest coach check, when each source last synced),
**Trends** (4 weeks, 12 weeks, 1, 2, 5 or 10 years – beyond a year one point per week: a sparkline
tile per metric, training hours per week, acute:chronic load with the 0.8–1.3 band) and one
**metric page** per tile (`/metrics/resting_hr`, `/metrics/hrv`, sleep, sleep score, stress, Body
Battery, steps, intensity minutes, training load, SpO2, respiration, weight, VO2max): the full
chart of watch and strap with the 28-day band (mean ± 1 SD of the preceding days) and the 7-day
mean, zoom, a second metric to compare below, the day's curve for the sample-based metrics (5-minute
slots, nights shaded, stress by band; click a day or step through them), the weekday pattern and a
daily table. **Activities** (period, sport and name filter, hours per week by sport, Garmin duplicates from USB
and phone merged; detail with linked heart-rate, pace or speed, elevation, cadence and power curves,
a zone donut, the lap chart, the track drawn from the GPS points without a map, laps, FIT download)
and **Sleep** (watch and strap as bed-to-wake bars per night; detail with the hypnogram, the night's
heart-rate, HRV, respiration and SpO2 curves on one time axis, stage shares against the 28-day mean,
and the other device's night).
Missing days stay gaps. Light and dark follow the system; a toggle in the navigation (desktop) and
under Settings → Appearance pins one, and the EN/DE switch next to it keeps the language for this
browser (the config's `[ui] language` stays the default). Settings also checks the coach backend
(is Ollama up and the model pulled, is the API key valid) as a background job. **Coach** starts a daily check, a weekly review, a question or a dry run (the prompt goes to
`coach/dryrun/`, nothing is sent) in the background (one at a time; the page shows the progress and
reloads when the answer is there), lists the recent runs with tokens, cost, duration and rating
buttons (+1/0/−1, also under the daily check on Today), edits the goal profile (`/coach/profile`,
open placeholders are counted), runs the goal wizard as a form (`/coach/wizard`, your own notes
survive) and keeps the journal. **Workouts** lists the workout files, creates new ones from the time, distance or
interval template, drafts the week from the coach's plan, edits a file as Markdown with a live
check (the steps as the watch will show them, name clashes in the same week), pushes the ticked or the
upcoming files to the watch through the device agent (dry run, force, schedule file), verifies what the
watch took over, deletes a file after a confirmation and shows the push history; `/workouts/watch` reads
the watch's own workout folders and removes a workout after a confirmation (backup first). It shows the same
numbers as CLI and TUI because it calls the same library functions
([concept](docs/dashboard-concept.md)). **Sync** starts the device jobs from the browser: import
from the watch, pull the phone, pull the strap (each with the options of the command line, a live
log, a stop button and the equivalent command to copy), and shows whether the watch or the phone is
attached. Those jobs do not run in the web container, which has no device access: the **device
agent** (Compose service `agent`, `wristpull agent`) runs next to it with the rights of the `cli`
service, takes the jobs from a queue in the database and writes log and result back; Today shows
whether it is online. **Insights** (`/insights`) is the analysis page: a readiness index 0–100 computed from your own
28-day baselines (HRV, resting HR, sleep, sleep score, Body Battery, yesterday's stress, acute:chronic
load) with every component's contribution shown, the acute:chronic ratio, sleep regularity (bedtime and
wake-up spread, sleep debt against `[coach] sleep_target_h`), relationships between sleep, load, stress,
Body Battery and HRV as Spearman correlations of the within-person z-scores with lags 0–2 and a binned
response curve that names the sweet spot when one bin clearly beats its neighbours, and day tags (alcohol,
sauna, sick, travel, …) whose effect on the following day is compared with the other days. Today shows the
readiness and the day's tags; `wristpull coach tag 2026-11-14 +sauna` sets tags from the terminal. The
button "Create AI report" (also `wristpull coach insights`) hands exactly these statistics to the configured
coach backend, which narrates them in four sections (what the data shows, what it means, one thing to try,
numbers used) and never recomputes them; the last report stays on the page with its rating. `/report`
reads the weekly or monthly report in the browser with the period's charts.
**Days** is the TUI's day table (steps, resting HR, sleep, Body Battery,
stress, intensity minutes, strap steps, sources) for any period with the watch-and-strap detail of
one day. **Files** browses the data directory (raw archive, exports, reports, workouts): FIT files
are inspected (message types, counts, fields) with their archive row, text files previewed,
everything downloadable; `adb/` and `staging/` stay hidden. **Export** writes the Claude export
(Markdown + CSV) or a weekly or monthly report as a background job, lists the files with download
links and shows the Markdown preview; the activity page offers the FIT file for download, the
activity list filters by name, the sleep list by device. On the phone the pages that do not fit
into the bottom bar sit behind "More". The goal is every TUI function in the browser; the help page
(`/help`) lists every page with its TUI key and command-line equivalent ([concept](docs/dashboard-concept.md), "Parity with the TUI"); charts, analysis
(readiness, correlations, tags) and the AI insights report follow
[docs/analysis-concept.md](docs/analysis-concept.md).

**Docker (recommended, reachable from the phone over HTTPS):**

```sh
cp .env.example .env               # then fill in UID/GID, the two paths and WRISTPULL_SITE
docker compose up -d --build
# first login: the start password "wristpull"; the dashboard then asks for your own password
```

**Settings in the browser.** The Settings page holds everything else: the dashboard password, the
band's auth key, the coach API keys and the Ollama address (saved to `<data_dir>/secrets.json`, mode
0600, where the `cli` and `agent` services read them too), and the whole config file (time zone,
folders, phone, band, agent, coach). A value saved there overrides `.env` and the shell. Only what
Docker needs before the containers start stays in `.env` (UID/GID, the paths, `WRISTPULL_SITE`,
`WRISTPULL_USB_GID`); the page lists those read-only. The older way still works: a password hash from
`wristpull serve --hash-password` in `WRISTPULL_WEB_PASSWORD_HASH` and the keys in `.env`. Without any
password the dashboard accepts the start password `wristpull` exactly until you set your own, which
it insists on at that first login; without `WRISTPULL_WEB_SECRET` it generates a secret once and keeps
it in the store, so sessions survive restarts. The store is part of every backup (like `.env`).

**Backup and restore in the browser.** The Backup page (`/backup`, linked from Settings and the "More"
sheet) writes the same archive as `wristpull backup create` into `<data_dir>/backups/`: data
directory, config file, `.env` and the certificate files from `deploy/certs/` (compose mounts both
into the `web` container for this). The page lists the archives with their manifest, offers download
and a two-step delete, and takes an upload from another instance as a raw request body with a
progress bar (nothing passes through a form parser or `/tmp`); the archive is checked before it is
listed. "Restore" first shows what the archive holds and what this instance already has, then needs
the dashboard password. The restore runs as a job while the dashboard keeps serving: the archive's
files overwrite, the live database is overwritten through SQLite's backup API (open connections of
the dashboard and the agent stay valid), config and `.env` are rewritten in place, the configuration
is reloaded; nothing is deleted. Afterwards "Restart the dashboard" (inside Docker; the process exits
and is started again) applies the restored session secret and time zone, and `docker compose up -d`
on the host hands the restored `.env` and certificates to the agent and Caddy. On a fresh instance
the start-password login offers "Restore a backup instead": upload, restore, and log in with the
password of the backed-up instance (the start-password session ends the moment a real hash exists).

`.env` also names the data directory, the config file and `WRISTPULL_SITE`, the addresses Caddy
serves (for example `https://192.168.1.10:8443, https://localhost:8443`). Caddy terminates HTTPS
with its own local certificate authority; the app container has no published port. To make the
phone trust it, copy the root certificate out once and install it on the phone (Android:
Settings → Security → Encryption & credentials → Install a certificate → CA certificate):

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./wristpull-root.crt
```

Keep the `caddy_data` volume: it holds that CA, and a new one would have to be installed again.

**Your own CA instead:** if your devices already trust a CA of yours, issue a certificate whose
subject alternative names cover every entry of `WRISTPULL_SITE` (`DNS:localhost`, the laptop's host
name if you use one, `IP:127.0.0.1` and the LAN address), put the PEM files into `deploy/certs/`
(git-ignored) and set `WRISTPULL_TLS=/certs/wristpull.pem /certs/wristpull-key.pem` in `.env`; then
`docker compose up -d caddy`. Caddy reloads nothing by itself: after renewing the files run that
command again.

- **Password:** only its scrypt hash is stored (the settings page writes it to the secrets store;
  `WRISTPULL_WEB_PASSWORD_HASH` in `.env` is the alternative), never the password and never in the
  config file. Five failed logins from one address (twenty overall) lock the login for 15 minutes.
  Change it under Settings → Change password.
- **Session secret:** `WRISTPULL_WEB_SECRET` (or the one generated into the store) signs the session
  cookie, so you stay logged in across restarts (30 days).
- **One worker:** the server runs exactly one uvicorn worker, because the login limit (and later
  the background jobs) live in process memory. Do not scale the `web` service.
- Cookies are `Secure`, `HttpOnly`, `SameSite=Strict`; every form carries a CSRF token; a strict
  Content-Security-Policy allows scripts only from the app itself (htmx and ECharts are vendored,
  nothing is loaded from the internet).
- **Coach backend in the container:** the backend and model come from the mounted config file.
  OpenRouter and Claude need their API key (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`) either in `.env`
  or exported in the shell that runs `docker compose up`.
- **Ollama for coach runs from the container:** the container reaches the host as
  `host.docker.internal` (`WRISTPULL_OLLAMA_URL`, default `http://host.docker.internal:11434`). Never set `OLLAMA_HOST=0.0.0.0` (that opens Ollama to the whole LAN
  without authentication). Either bind Ollama to the docker0 address (`OLLAMA_HOST=172.17.0.1`;
  then the coach on the host must use `http://172.17.0.1:11434` as well, and the Ollama service
  needs `After=docker.service`), or keep it on localhost and allow port 11434 only from the Docker
  subnets with a firewall rule.

**Without Docker** (on the laptop only): `uv sync --extra web`, then `uv run wristpull serve` and open
`http://127.0.0.1:8765` (start password `wristpull`, or export `WRISTPULL_WEB_PASSWORD_HASH`). Options: `--host`, `--port`,
`--data-dir` (overrides `[storage] data_dir`), `--behind-proxy` (trust `X-Forwarded-For`, only
when a proxy is the sole way in).

**Updating** after `git pull`: `docker compose build && docker compose up -d web agent`. Only the app
and agent containers are recreated; Caddy and its certificate authority stay, and you stay logged in as long as
`WRISTPULL_WEB_SECRET` is unchanged. While the container runs it writes to the data directory:
coach runs, ratings, journal notes and edited workout files.

- **Device agent:** `agent` starts with `docker compose up -d` and needs the same host preparation
  as `cli` (udev rule, AppArmor profile, `WRISTPULL_USB_GID`, the strap key in `.env`): the setup
  script does all of it. It also imports by itself when the watch is plugged in (`[agent]
  import_on_plug`), and the device card says when the host's Bluetooth is switched off (rfkill),
  the usual reason for "No Bluetooth adapters found". Without the AppArmor profile only this service fails to start and the
  dashboard says "device agent offline"; the jobs then wait in the queue until it is back. The agent
  has no ports and listens on nothing. `wristpull agent --status` (through `cli`) prints its
  heartbeat and the current job, `wristpull agent --once` runs one cycle by hand. When the agent and
  `cli` (or the udev import unit) open the watch at the same time, the second one gets the usual
  "device busy" message and can simply be repeated.

**TUI key ↔ web page.** Every function of the terminal UI has a page in the dashboard (the parity rule,
checked by `tests/test_web_parity.py`):

| TUI | Web page | Notes |
|---|---|---|
| 1 Overview | `/` Today | readiness, tags, key values, day curve, devices, coach excerpt |
| 2 Devices & Sync | `/sync` | device jobs through the agent, log, stop |
| 3 Days | `/days` | period presets, day detail |
| 4 Activities | `/activities`, `/activities/<id>` | filters, weekly hours, curves, zones, laps, track, FIT download |
| 5 Sleep | `/sleep`, `/sleep/<id>` | bed-to-wake bars, hypnogram, night curves, stage shares |
| 6 Files | `/files`, `/files/download` | archive rows, FIT inspect, previews |
| 7 Export & Report | `/export`, `/report` | jobs with downloads; reports as pages |
| 8 Settings | `/settings` | config, keys, password, backend check, theme, language |
| 9 Coach | `/coach`, `/coach/profile`, `/coach/wizard` | runs, dry run, rating, journal, profile, wizard |
| 0 Workouts | `/workouts`, `/workouts/watch` | editor, push/verify/delete through the agent, watch view |
| ? Help, F12 | `/help`, EN/DE switch | page overview with command-line equivalents |
| – | `/trends`, `/metrics/<key>`, `/insights` | charts and analysis beyond the TUI ([analysis concept](docs/analysis-concept.md)) |
| – | `/backup` | the whole state as one archive: create, download, upload, restore into this instance (`wristpull backup`, beyond the TUI) |

## Running everything in Docker

The same image also runs every other command through the `cli` service of `compose.yaml`
([concept](docs/docker-concept.md)): the TUI, export, report, coach, workouts, `pull phone`
over ADB, `pull strap` over Bluetooth and `import garmin` over MTP. `web` stays as it is: reachable from the
network, no USB. `cli` is started per command, has USB pass-through and the host network, and runs
as your user so files in the data directory keep their owner. The `agent` service is a long-running
`cli`: it executes the device jobs the dashboard enqueues (see "Web dashboard").

```sh
contrib/bin/wristpull-setup                # guided: does everything below and asks only for what is missing
# or by hand:
cp .env.example .env                       # UID/GID, WRISTPULL_DATA, WRISTPULL_CONFIG (+ dashboard secrets)
mkdir -p ~/Health/wristpull && touch ~/.config/wristpull/config.toml   # the config is bind-mounted as a file
mkdir -p ~/Health/wristpull/adb && cp ~/.android/adbkey* ~/Health/wristpull/adb/   # once, if the phone already trusts this laptop
docker compose build
docker compose run --rm cli wristpull config --init     # fills the empty file with the defaults
docker compose run --rm cli wristpull status
docker compose run --rm cli wristpull tui
docker compose run --rm cli wristpull pull phone
docker compose run --rm cli wristpull pull strap
contrib/bin/wristpull tui                  # wrapper for the line above (works from the checkout)
```

- **What is inside:** the extras `web`, `coach`, `tui`, `ble`, plus `adb`, `mtp-tools` (libmtp) and
  `nano` and `micro` (`EDITOR` from your shell picks one). `TERM`, `COLORTERM`, `LANG`, `EDITOR` and `VISUAL` come from your shell; the config file
  is mounted read-write (the TUI settings screen saves it) and found through
  `WRISTPULL_CONFIG_FILE`; the data directory is mounted at `/data` (`WRISTPULL_DATA_DIR`
  overrides `[storage] data_dir`, the file keeps the host path). The TUI's `o` key cannot open a
  desktop program from the container, so it shows the path on the host instead.
- **Phone (ADB):** the adb server runs inside the container; the keys live in
  `<data_dir>/adb/` (`adbkey`, `adbkey.pub`). Seed them from `~/.android/` as above, otherwise
  the phone asks once for a new authorisation. An adb server already running on the host is
  simply used (`cli` shares the host network, port 5037); when adb sees no device although an
  ADB-capable phone is attached, `pull phone` says so and points at the authorisation prompt and
  `adb kill-server`. Wireless debugging (Android 11+) works with `[phone] serial = "192.168.1.20:37099"`
  (`adb connect` runs first); Android drops it after a reboot or network change, so it is an
  extra, not the default for the morning timer.
- **USB access:** the service mounts `/dev/bus/usb` with the cgroup rule `c 189:* rmw`, which
  grants access to every USB device (keyboard, sticks, phone); acceptable for a command you
  start, which is why it is never given to `web`. It never uses `privileged`. The device nodes
  must be readable by your user: the desktop's `uaccess` ACL covers an active session, the udev
  rule `contrib/udev/91-wristpull-garmin-docker.rules` sets `GROUP="plugdev"` for runs without one
  (and keeps the desktop away from the watch); `WRISTPULL_USB_GID` in `.env` names that group
  (default 46).
- **Strap (Bluetooth):** bleak talks to the host's `bluetoothd` over the system D-Bus, so `cli`
  mounts `/run/dbus/system_bus_socket`. Docker's default AppArmor profile denies D-Bus calls
  (`org.freedesktop.DBus.Error.AccessDenied … AppArmor policy`), so `cli` runs under
  `contrib/apparmor/wristpull-cli`: the default profile plus the system-bus rule, installed once with
  `apparmor_parser` (see `contrib/apparmor/README.md`; without it Docker refuses to start `cli`). The
  host user must be allowed to use BlueZ (a desktop session, or the `bluetooth` group). `web` has no
  Bluetooth. Linux only, like USB.
- **Coach:** `cli` runs with `network_mode: host`, so `[coach] ollama_url =
  "http://localhost:11434"` works unchanged; API keys come from `.env` or your shell as for `web`.
  Never set `OLLAMA_HOST=0.0.0.0` and never run `adb -a`.
- **Watch (MTP):** the watch is read with libmtp's command-line tools (`mtp-files`,
  `mtp-getfile`, …) behind `src/wristpull/mtp.py`, in the container and natively alike (then
  `apt install mtp-tools`, and nothing else may hold the device, see "Connecting the fenix 6").
  Every call opens the device again (about six seconds on a fenix 6X Pro), so `import garmin`
  lists once, skips files already archived by name and size, downloads the rest into
  `<data_dir>/staging/` and archives them from there with the usual SHA-256 check; `workout push`,
  `verify`, `watch` and `remove` work the same way (`target` in the push history reads
  `mtp://GARMIN/NewFiles/<file>`). The tools report no modification time, so `device_mtime_utc`
  stays empty for these files. `status` says "connected over USB (MTP)". Measured values and the
  full spike are in [docs/docker-concept.md](docs/docker-concept.md).
- **Limits:** Linux only: Docker Desktop on macOS and Windows passes neither USB nor Bluetooth
  into containers; there the native installation reaches the devices
  ([docs/platforms.md](docs/platforms.md)).
- **Updating:** `git pull && docker compose build` rebuilds the one image for all three services;
  `docker compose up -d web agent` restarts the dashboard and the agent.
- **Moving to another machine or restoring a backup:** the dashboard's Backup page (or
  `contrib/bin/wristpull-backup`) writes the whole state (data directory, config file, `.env`,
  certificate files) as one tar.gz; on the new machine either `contrib/bin/wristpull-restore <archive>`
  right after the clone (builds the image, unpacks everything, writes `.env` for that user), or
  `docker compose up -d` on a fresh `.env`, log in with the start password and upload the archive on
  the Backup page. [docs/setup.md](docs/setup.md) step 10 has the details and what Caddy regenerates.

## Automation

Devices that are only sometimes on the cable do not fit timers. `contrib/bin/wristpull-morning` walks
through the routine instead: plug the watch in (import, optional workout push), unplug it, pull the
strap over Bluetooth (`pull strap`), optionally run the daily coach – each step waits for the device
and can be run on its own (`wristpull-morning strap`). The Gadgetbridge way (sync in the app, plug the
phone in, `pull phone`) is still there as the `phone` step for anyone who keeps the app.


With the device agent running (`docker compose up -d`), plugging the watch in is enough: the agent
sees it on USB, waits ten seconds for the fenix's second enumeration and imports (`[agent]
import_on_plug`, on by default; the Sync page shows the job). `contrib/systemd/` has user units for
anyone who wants timers anyway (`pull phone` at 06:45, `coach daily` at 07:00, each `docker compose
run --rm -T cli …`) and the older oneshot import service that
`contrib/udev/91-wristpull-garmin-docker.rules` can start when the watch is plugged in (not next to
the agent); the rule itself is still needed: it makes the device readable for `plugdev` and hides
the watch from the desktop's MTP handling, and the setup script installs it. Both READMEs explain
installation; `loginctl enable-linger` lets everything run without a logged-in session. Keep the
order the watch dictates:
USB import before any phone app syncs, because Garmin deletes monitoring files once they were fetched.
While the watch is on the cable anyway, `wristpull workout push --upcoming` copies the planned
workouts over; the import service template shows where that step belongs.

## Website and screenshots

`docs/` carries the static project website (planned home: wristpull.de): `index.html` (the story
and the three reasons to switch), `features.html`, `hub.html` (the planned wristpull Hub, with the
beta contact), `install.html` and `roadmap.html`, sharing `wristpull.css`. Fonts are self-hosted
under `docs/fonts/` (`@fontsource/source-sans-3`), so the site makes no external requests and sets
no cookies. `docs/screenshots/` holds the pseudonymised TUI renders, produced from the synthetic
test fixtures only, and `docs/logos/` the logo; every chart on the site shows synthetic data. The
`docs/` folder is laid out for GitHub Pages ("deploy from a branch", folder `/docs`; `.nojekyll`
keeps the files as they are). Locally: `python3 -m http.server 8000` from the repository root and
open `/docs/`.

## Data layout

```
~/Health/wristpull/
├── wristpull.sqlite
├── secrets.json                    password hash, session secret, keys saved on the settings page (0600)
├── backups/                        archives of the Backup page and `wristpull backup create` (never inside an archive)
├── raw/<source>/<year>/<kind>/<sha256-prefix>_<original name>
├── exports/<from>_<to>/            summary.md + CSVs (wristpull export)
├── reports/<week|month>-<label>/   report.md (+ SVG charts)
├── coach/
│   ├── profile.md                  the goal profile sent to the coach (edit it)
│   └── dryrun/<timestamp>_<kind>.md  full prompts written by --dry-run
└── workouts/
    ├── <date>-<slug>.md            one workout per file (wristpull workout new / from-plan, or by hand)
    ├── out/                        FIT files of a dry-run push
    └── watch-backup/               copies of workout files removed from the watch
```

`source` is `garmin_usb`, `gadgetbridge_garmin`, `garmin_connect`, `zepp_ble` or `gadgetbridge_zepp`; the exported Gadgetbridge
database itself is archived under `gadgetbridge/<year>/database/`. Every raw file is identified by
its SHA-256 (`raw_files` table), stored read-only and never overwritten; an import is idempotent and
can be re-run at any time. Coach answers are not files but rows in `coach_runs`
(`wristpull coach show`); pushes of workouts to the watch are rows in `workout_pushes`.

### Sources and how they relate

| source | device | what is read | notes |
|---|---|---|---|
| `garmin_usb` | fenix | FIT files from the watch over USB | complete, including activities with per-second records |
| `gadgetbridge_garmin` | fenix | the same FIT files, fetched by Gadgetbridge over Bluetooth and pulled from the phone | same parser; the watch splits them into smaller files |
| `garmin_connect` | every Garmin device the account ever had | the FIT files of the Garmin Connect account export plus its daily summaries and nights (`import garmin-export`) | same parser for FIT; daily aggregates in `connect_daily`; fills the history from before wristpull, lowest priority in the merge |
| `zepp_ble` | Helio Strap | fetched from the band over Bluetooth by wristpull (`pull strap`) | the band's own byte buffers, archived as raw files; first choice for the strap |
| `gadgetbridge_zepp` | Helio Strap | tables of the exported Gadgetbridge database | per-minute samples, no raw files exist; fills gaps behind `zepp_ble` |

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

`garmin_usb`, `gadgetbridge_garmin` and `garmin_connect` describe the same watch, so their
readings overlap. They are kept apart in the sample tables and in `daily_summary` (one row per
source and day). The export and report layer merges them (`merge.py`): daily metrics are
coalesced column by column, USB first, Gadgetbridge fills gaps, the Connect export comes last;
activities and nights are de-duplicated by start minute. The Helio Strap is a separate device and
stays separate throughout (`strap_*` columns); its two sources are merged the same way, `zepp_ble`
first and `gadgetbridge_zepp` filling gaps.

### Tables

All timestamps are ISO-8601 UTC strings (`2026-10-31T06:12:33Z`). Every sample row carries `source`,
`tz` (IANA zone from the config) and `local_date` (the calendar day in that zone).

| Table | Content (Garmin FIT message) |
|---|---|
| `raw_files` | archive index, parse status, FIT file type, device serial |
| `activities` | one row per session: sport, duration, distance, HR, speed, ascent, training effect, training load (`training_load_peak`), HR zone times as JSON, `name` from the Garmin Connect export when known |
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
| `metrics` | sparse metrics: `vo2_max` (Garmin `max_met_data`, Connect export), `weight_kg` (Connect export), `pai_total` / `pai_today` (Helio Strap) |
| `connect_daily` | the Garmin Connect export's daily aggregates (steps, HR, stress, body battery, SpO2, respiration per day); `daily_summary` takes them only where the samples left NULL |
| `daily_summary` | derived per day and source, rebuilt after every import |
| `coach_runs`, `coach_feedback`, `journal` | training-coach runs (prompts, answer, tokens, cost), the user's ratings, a daily note |
| `workout_pushes` | every workout FIT written to the watch: name, date, SHA-256, target path, watch serial, status `dry_run` / `pushed` / `verified` |
| `day_tags` | the day tags of the insights (date, tag), schema version 8 |

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
uv sync --extra tui --extra coach --extra web   # create .venv incl. TUI, Claude SDK and web dashboard
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

The web dashboard (`src/wristpull/web/`) follows the same rule (`tests/test_web.py` checks it):
page data in `pages.py`, routes and login in `app.py`, background coach runs in `jobs.py`, strings
from the shared catalog translated per request. Its tests use Starlette's `TestClient` on
synthetic data and a `FakeBackend`.

The coach (`src/wristpull/coach/`) keeps SQL in `store.py` and `context.py`; everything the model
sees is built from the aggregate functions, and all tests talk to a `FakeBackend` (no network). The
Claude backend is exercised against a fake client, Ollama and OpenRouter against a fake `urlopen`.

Tests use **synthetic** FIT files generated with `wristpull.fit.writer` (the minimal FIT encoder that
also writes the workout files; `tests/fitwriter.py` re-exports it), so no
real health data is needed or allowed in the repository. Code, comments and commits are in English.
See [CONTRIBUTING.md](CONTRIBUTING.md); the licence is MIT ([LICENSE](LICENSE)). An opt-in live test
(`WRISTPULL_LIVE=1`) calls a real LLM backend; everything else stays offline.

## Roadmap

1. ~~Garmin USB import, FIT to SQLite (activities, monitoring, sleep), `status`~~
2. ~~`pull phone`: Gadgetbridge database and FIT files via ADB, Helio Strap parsing~~ (done before wave 2 because the devices were set up first)
3. ~~`export --for-claude --from --to` (compact Markdown + CSV) and `report week|month`, merging the Garmin sources~~
4. ~~Verify sleep/HRV/SpO2 mappings against a real night~~ (2026-11-01, see verification status) — next: udev/systemd trigger for automatic imports
5. ~~Terminal UI (`wristpull tui`): overview, sync jobs with cancel, days, activities, sleep, files, export & report, editable settings; German/English~~ — concept in [docs/tui-concept.md](docs/tui-concept.md), screenshots in [docs/screenshots/](docs/screenshots/README.md)
6. ~~Training coach via LLM: C1 daily check and questions with Ollama, goal profile, local memory; C2 weekly review, Claude API and OpenRouter backends, number check, ratings; C3 goal wizard, TUI coach screen; C4 journal, plan adherence, ICS export, systemd/udev templates~~ — concept in [docs/coach-concept.md](docs/coach-concept.md)
7. ~~Workouts to the watch: Markdown workouts, FIT encoder, `workout push|verify|from-plan`, TUI screen 0; `NewFiles` import confirmed on the fenix 6X Pro~~ — interval blocks (`repeat`) added; next: the schedule file (calendar date)
8. Web dashboard in Docker (phone in the home network, HTTPS + password): concept in [docs/dashboard-concept.md](docs/dashboard-concept.md); ~~D1 login, Today, Trends, Docker + Caddy~~, ~~D2 activities and sleep~~, ~~D3 coach runs, rating, journal, workout editor~~ — later: moving to a server (raw-file transfer + import there)
9. ~~Everything in Docker: one image for the dashboard and the `cli` service (USB pass-through, the watch over the MTP tools without gvfs, the phone over ADB, udev rule and units for the container)~~ — concept in [docs/docker-concept.md](docs/docker-concept.md); next: publishing the image
10. ~~Garmin Connect account export: `import garmin-export` (every uploaded FIT file, daily summaries, nights with scores, activity names, VO2max and weight history)~~ — the merge keeps the live sources first
11. ~~Helio Strap over Bluetooth without Gadgetbridge: Zepp OS protocol (B-163 + AES), `pull strap`, `strap scan|pair|info`, archive + reparse of the band's buffers, workouts, PAI and maximum heart rate~~ — concept in [docs/strap-ble-concept.md](docs/strap-ble-concept.md); the standalone box (Raspberry Pi) is a separate project
12. Later: MCP server / Claude skill over the SQLite, nutrition source (MyNetDiary or own logging)
