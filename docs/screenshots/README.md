# Screenshots

Headless renders of the terminal UI (`wristpull tui`), produced from the **synthetic test fixtures**
(`tests/conftest.py`, `tests/gb_db.py`) with Textual's `save_screenshot`. No real health data: every
number, date and name is invented, device identifiers are placeholders, the home directory is
`/home/alice`. They illustrate the layout in the README and on the project page `docs/index.html`.

| File | Screen |
|---|---|
| `01-overview.svg` | 1 Overview |
| `02-sync.svg` | 2 Devices & Sync after an import job |
| `03-days.svg` | 3 Days |
| `04-activities.svg` | 4 Activities |
| `05-sleep.svg` | 5 Sleep |
| `06-files.svg` | 6 Files with FIT inspection |
| `07-export.svg` | 7 Export & Report after an export |
| `08-settings.svg` | 8 Settings |
| `09-coach.svg` | 9 Coach after a daily check (fake backend) |
| `10-workouts.svg` | 0 Workouts after pushing one file to a fake watch folder |

Regenerate with a script like the one used in the session (import the fixtures, run the app with
`App.run_test`, call `save_screenshot`), then scrub paths: `sed -i 's#/home/<you>#/home/alice#g' *.svg`.

## Web dashboard

`web-*.jpg` are screenshots of the web dashboard (`wristpull serve`) taken in Chrome at about 980 px
width against a throwaway data directory seeded from the same synthetic fixtures (60 days of monitoring
with invented day-to-day variation, 20 runs, one night per day, a fake coach answer, a few day tags).
Nothing in them is real; the host path shown is `/home/alice/Health/wristpull`.

| File | Page |
|---|---|
| `web-today.jpg` | Today: readiness, tags, key values with sparklines, the coach excerpt |
| `web-trends.jpg` | Trends: one sparkline tile per metric, training load |
| `web-metric.jpg` | A metric page: baseline band, zoom, comparison, day curve |
| `web-insights.jpg` | Insights: readiness components, acute:chronic load, sleep regularity |
| `web-night.jpg` | A night: hypnogram, night curves, stage shares |
| `web-activity.jpg` | An activity: linked curves, track, zones, laps |

Regenerate: seed a temporary directory with the fixtures (`tests/conftest.py` builders, one monitor and
sleep file per day, an activity every third day), start `wristpull serve --host 127.0.0.1 --port 8765`
with a throwaway password hash and `WRISTPULL_HOST_DATA_DIR=/home/alice/Health/wristpull`, and
screenshot the pages.
