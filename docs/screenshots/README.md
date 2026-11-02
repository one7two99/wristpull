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
