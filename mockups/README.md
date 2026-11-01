# TUI mockups

Text mockups for the planned `wristpull tui` terminal interface. They are design
artefacts, not screenshots: all numbers are invented, device identifiers are
placeholders (`<serial>`, `<id>`), and no real health data is shown.

The concept behind these screens is in [`docs/tui-concept.md`](../docs/tui-concept.md).

Implementation status (2026-11-01): all eight screens, the help overlay and the command palette
exist in `wristpull tui`. The mockups remain the design reference; details differ where the data
suggested otherwise (for example the sleep screen lists every source instead of pairing rows).

## Files

| File | Screen | Shows |
|------|--------|-------|
| `00-navigation.txt` | Screen map | How the eight screens, the command palette and the detail views connect |
| `01-dashboard-de.txt` | 1 Overview (German) | Device status, last import per source, 7-day KPIs with sparklines, recent activities, notes |
| `01-dashboard-en.txt` | 1 Overview (English) | Same screen in English, demonstrating the i18n switch (`F12`) |
| `02-sync.txt` | 2 Devices & Sync | Import/pull forms, a running `pull phone` job with live log, last result, CLI equivalent |
| `03-daily.txt` | 3 Days | `daily_summary` table for a week with source toggle and a per-day detail pane |
| `04-activities.txt` | 4 Activities | Filterable list, detail with HR zones, HR curve and laps |
| `05-sleep.txt` | 5 Sleep | Nights with stage bars on a 22–08 h axis, Garmin and strap rows side by side |
| `06-files.txt` | 6 Files | Raw archive tree, file metadata and the `inspect` message summary |
| `07-export-report.txt` | 7 Export & Report | Form, result file list and Markdown preview |
| `08-settings.txt` | 8 Settings | Config fields, `[ui]` section, mapping verification status, session log |
| `09-palette-help.txt` | Palette and help | Command palette with CLI equivalents, key-binding overlay |
| `10-narrow-80x24.txt` | 1 Overview at 80×24 | How the overview collapses on a small terminal |

## Conventions

- Width 100 columns (the narrow variant 80), box drawing for frames.
- `●` connected, `○` indirect (via Gadgetbridge database), `✓` verified, `[!]` warning, `[i]` note.
- `▁▂▃▄▅▆▇█` sparklines; `▓ ▒ ░ ·` deep, light, REM, awake in sleep bars.
- `[x]`/`[ ]` checkbox, `(•)`/`( )` radio, `[ value ▾ ]` dropdown, `[ text ]` input, `▶` selected row.
- Every screen ends with a key bar; every action shows its CLI equivalent so it can be scripted.
- Date and number formatting follow the active language (German: `31.10.`, `7,8 MiB`;
  English: `Oct 31`, `7.8 MiB`). Timestamps in the database stay UTC ISO-8601.

The mockups were rendered from a small generator script that pads every line to the
frame width; the script is not part of the repository.
