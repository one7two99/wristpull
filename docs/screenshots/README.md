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

`web-*.jpg` are screenshots of the web dashboard (`wristpull serve`) taken in Chrome at 1248 × 681 px
against a throwaway data directory seeded from the same synthetic fixtures: 60 days of monitoring with
invented, seeded day-to-day variation (correlated on purpose, so the relationship charts have something to
show), a run every third day, one night per day, fake coach answers, two workout drafts, a few day tags.
Nothing in them is real; the host path shown is `/home/alice/Health/wristpull`.

| File | Page |
|---|---|
| `web-today.jpg` | Today: readiness with its reasons, today's tags, key values with sparklines |
| `web-trends.jpg` | Trends: one sparkline tile per metric |
| `web-metric.jpg` | A metric page: baseline band, 7-day mean, zoom |
| `web-insights.jpg` | Insights: the readiness components with value, z, score, weight and contribution |
| `web-relationships.jpg` | Insights: relationships with bin means and sweet spots |
| `web-sleep.jpg` | Sleep: nights as bed-to-wake bars |
| `web-night.jpg` | A night: hypnogram and the heart-rate curve |
| `web-activities.jpg` | Activities: filters and hours per week |
| `web-activity.jpg` | An activity: linked curves, track, zones, laps (taller capture) |
| `web-days.jpg` | Days: the day table with the watch/strap detail |
| `web-files.jpg` | Files: a folder of the raw archive |
| `web-report.jpg` | A weekly report as a page with charts |
| `web-coach.jpg` | Coach: runs, the insights report, profile and wizard links |
| `web-workouts.jpg` | Workouts: push, verify, delete through the device agent |
| `web-settings.jpg` | Settings: password, keys, config |
| `web-sync.jpg` | Devices & Sync: the three device cards |
| `web-help.jpg` | Help: every page with its TUI key and command line |

Regenerate: seed a temporary directory with the fixtures (`tests/conftest.py` builders, one monitor and
sleep file per day, an activity every third day, seeded random variation written into `daily_summary` and
`sleep`), start `wristpull serve --host 127.0.0.1 --port 8765` with a throwaway password hash and
`WRISTPULL_HOST_DATA_DIR=/home/alice/Health/wristpull`, and screenshot the pages. Never screenshot a page
that shows a real path or a real value.
