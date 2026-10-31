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
```

## Usage

Run every command as `uv run wristpull ...` from the repository, or install the script once with
`uv tool install --editable .` and call `wristpull ...` directly. The examples below use the short
form.

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
```

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

## Data layout

```
~/Health/wristpull/
├── wristpull.sqlite
└── raw/<source>/<year>/<kind>/<sha256-prefix>_<original name>
```

`source` is `garmin_usb`, `gadgetbridge_garmin` or `gadgetbridge_zepp`; the exported Gadgetbridge
database itself is archived under `gadgetbridge/<year>/database/`. Exports land in
`<data_dir>/exports/`, reports in `<data_dir>/reports/`. Every raw file is identified by
its SHA-256 (`raw_files` table), stored read-only and never overwritten; an import is idempotent and
can be re-run at any time.

### Sources and how they relate

| source | device | what is read | notes |
|---|---|---|---|
| `garmin_usb` | fenix | FIT files from the watch over USB | complete, including activities with per-second records |
| `gadgetbridge_garmin` | fenix | the same FIT files, fetched by Gadgetbridge over Bluetooth and pulled from the phone | same parser; the watch splits them into smaller files |
| `gadgetbridge_zepp` | Helio Strap | tables of the exported Gadgetbridge database | per-minute samples, no raw files exist |

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

Verification status (fenix 6X Pro, 2026-10-31): activity files (sessions, records with
GPS/altitude/HR, laps, HR zone times, training load) and the wellness fields steps, all-day HR,
stress, body battery, resting HR and respiration are confirmed against real data; body battery was
cross-checked with Gadgetbridge's own import. Sleep stages, HRV and SpO2 (Garmin) and the Helio
Strap's sleep, stress and SpO2 tables follow the FIT profile and Gadgetbridge's layout but still
await a real night; use `wristpull inspect` on a fresh `Sleep/` file to check.

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
uv sync                                  # create .venv and install dependencies
uv run pytest -q                         # run the test suite
uv run pytest -q tests/test_fit_garmin.py -k sleep   # a subset
uv run pyflakes src tests                # lint
uv run wristpull --help                  # run the CLI from the checkout
uv tool install --editable .             # optional: `wristpull` on PATH, follows the checkout
uv lock --upgrade                        # refresh uv.lock
```

Tests use **synthetic** FIT files generated by `tests/fitwriter.py` (a minimal FIT encoder), so no
real health data is needed or allowed in the repository. Code, comments and commits are in English.

## Roadmap

1. ~~Garmin USB import, FIT to SQLite (activities, monitoring, sleep), `status`~~
2. ~~`pull phone`: Gadgetbridge database and FIT files via ADB, Helio Strap parsing~~ (done before wave 2 because the devices were set up first)
3. ~~`export --for-claude --from --to` (compact Markdown + CSV) and `report week|month`, merging the Garmin sources~~
4. Next: verify sleep/HRV/SpO2 mappings against real nights, udev/systemd trigger for automatic imports
5. Later: MCP server / Claude skill over the SQLite, dashboards, nutrition source (MyNetDiary or own logging)
