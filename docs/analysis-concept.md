# wristpull analysis and charts – concept

Status: concept approved 2026-11-14; wave A1 (metric pages, chart engine, theme toggle, Markdown
coach answers) implemented the same day. The waves are listed at the end; the TUI parity waves of
[dashboard-concept.md](dashboard-concept.md) ("Parity with the TUI") are woven into the same
sequence because the full TUI feature set in the browser is a hard requirement.

## Why

The dashboard showed six tiles on Today, eight small charts on Trends and detail pages for
activities and nights, but no page per metric: nobody could look at a year of resting heart rate
with a baseline, zoom in, or see the day's curve. Much of the stored data appeared nowhere
(intraday heart rate, stress and Body Battery, HRV status, steps, intensity minutes, SpO2,
respiration, sleep score over time, pace and cadence of an activity), the coach's Markdown
rendered as raw text, and the only analysis was the acute:chronic ratio, 7/28-day deltas and five
warning flags. The user asked (2026-11-14) for charts and analysis on the level of the big
providers, a UI that works on the desktop and the phone, and AI reports that relate sleep,
strain, stress, Body Battery and health to find the personal sweet spot.

## What the big providers show (checked 2026-11-14)

- **Garmin Connect**: one page per metric with day/week/month/year, intraday curves (heart rate,
  stress, Body Battery as an area with charge and drain phases), a 7-day average line, HRV status
  with a baseline band, a sleep score with a hypnogram and night curves, training status and load
  with an optimal range, and Training Readiness (0–100 from sleep, recovery time, HRV status,
  acute load and the sleep and stress history; Prime/High/Moderate/Low/Poor) with a breakdown of
  the contributions ([manual](https://www8.garmin.com/manuals/webhelp/GUID-2C274FD2-F0C3-445C-B0AC-700FECCE12E9/EN-GB/GUID-C21BE0C8-A08E-4DA1-B6C6-2E0E2DDDB372.html)).
- **Whoop**: Recovery (HRV, resting HR, respiration, sleep performance), Strain 0–21, Sleep Need,
  and the Journal with an "impact" analysis per habit
  ([comparison](https://www.gneta.app/blog/recovery-scores-compared-garmin-whoop-oura)).
- **Oura**: Readiness (resting HR, HRV balance, temperature, sleep, regularity, activity balance),
  Trends with a second variable to compare, Tags and "Discoveries" ("more REM after Sauna"), a
  baseline from 14 of 30 days, with the note that a correlation is not a cause
  ([tags](https://support.ouraring.com/hc/en-us/articles/360038676993-Using-Tags),
  [trends](https://ouraring.com/blog/he/trends/)).
- **Intervals.icu / HRV4Training**: fitness/fatigue/form (exponential means over 42/7 days), the
  HRV trend against a normal range, readiness as acute versus chronic, response types
  ([HRV4Training](https://marcoaltini.substack.com/p/hrv4training-pro-overview-page)).
- **Research on the sweet spot**: in population data sleep duration explains under 1 % of the
  next day's HRV variance; what matters is the deviation from one's own normal, regularity, and
  lags of 0–2 days ([Terra](https://www.tryterra.co/research/think-a-good-hrv-score-follows-a-good-night-sleep-think-again)).
  For wristpull: within-person z-scores against the own 28-day baseline, lags checked, small
  effects called "weak", a minimum n.

None of them publishes its formulas. wristpull does the opposite: every number can be recomputed
and the page shows what it is made of.

## Decisions

1. **Metric pages** `/metrics/<key>` sit behind the Trends overview, which became a grid of
   sparkline tiles (latest value, delta against the 28-day mean, link); the Today tiles link there
   too. A page is one large chart with the period, zoom, the baseline band, a second metric to
   compare, the day's curve for the sample-based metrics, a key-value strip, the weekday pattern
   and the daily table.
2. **The chart engine stays ECharts from Python definitions** (`web/pages.py` → `chart_json` →
   `static/app.js`), extended by chart kinds (`sparkline`, `intraday`, series roles for the
   baseline band and the 7-day mean), `dataZoom`, a tooltip with the unit, and a theme toggle;
   no further JavaScript framework. **No dual-axis charts**: a comparison is a second chart with
   the same days below the first ("same days, own scale"). The palette was validated for
   colour-vision deficiency and contrast in both modes (light `#0f8f80 #c46a1f #6250d6`, dark
   `#239d8b #d27a33 #8f82e6`); the band and the 7-day mean are chrome, not series.
3. **Analysis layer `src/wristpull/insights/`** (wave A3): rolling baselines and z-scores
   (polars, trailing, 7/28 days), a rolling ACWR, a readiness index 0–100 with per-component
   contributions, sleep regularity and debt, correlations with lag and a binned response curve
   (the sweet spot), the weekday pattern and tag effects. Pure functions over frames; SQL only
   through `merge`/`aggregate` and `insights/store.py` (tags).
4. **Tags**: a fixed list (`insights/tags.py`: alcohol, caffeine_late, late_meal, sick, travel,
   sauna, nap, alcohol_free, stress_high, rest_day, race) set per day in the browser and by
   `wristpull tag`, table `day_tags` (schema v8). The journal stays free text.
5. **The AI report "Insights"** is a fourth coach kind: the insights package is rendered into
   sections, the model interprets and never computes, names strength and n, does not confuse
   correlation with cause; the number check stays. The backend is whatever `[coach] backend`
   says (Ollama, Claude or OpenRouter), the model `[coach] insights_model`, else the weekly
   model, else the default. The statistics are recomputed on every page load in milliseconds
   from `daily_summary`; the report costs tokens only when the user presses the button (or runs
   the CLI), and the page shows the last stored report with its date.
6. **UI polish belongs to every wave**: mobile first, 44 px targets, scrollable period chips, a
   theme toggle (system/light/dark, `data-theme` + `localStorage`, applied by `theme.js` before
   the first paint), a Markdown renderer for coach answers (`web/markdown.py`, stdlib, everything
   escaped), a table alternative to every chart, `aria-label`s with the key statement.
7. **Full TUI parity is a requirement**: Days, Files, Export/Report, coach check, profile editor,
   wizard, dry run, language switch, workouts through the agent with the watch view, Help and the
   navigation are waves P2–P5 of this sequence; `tests/test_web_parity.py` will check that every
   TUI screen and job kind has a web route, and the README gets the table "TUI key ↔ web page".
8. **Out of scope**: map tiles (external), machine learning, Garmin fields the fenix 6 does not
   deliver (Training Readiness, Endurance Score), comparisons between users, backup in the browser.

## Pages and charts

### Trends (overview)
A grid of tiles (2/3/4 columns), one per metric in `series.METRICS`: title, latest value with the
delta against the 28-day mean (coloured by the metric's "good" direction), a sparkline over the
period, link to the metric page. Weight and VO2max only when the period has readings. Below: the
training hours per week and the acute:chronic ratio with the 0.8–1.3 band.

### Metric page `/metrics/<key>?weeks=&day=&compare=`
- Head: name, unit, a one-sentence explanation; key-value strip: latest (with date), 7-day and
  28-day mean before the latest day, lowest and highest in the period with dates, today's z-score.
- Main chart (320 px, 250 on the phone): watch and strap as two series, the 28-day band (mean ± 1 SD
  of the preceding days, computed over the 28 days before the window too so it exists from the
  first day), the 7-day mean as a thin muted line, a zoom slider from 15 points, a tooltip with
  the unit; beyond a year one point per ISO week (means, sums for volumes, last for readings).
- Compare: a select (allow-list) draws a second chart with the same days below; the default
  partner per metric (HRV ↔ sleep, stress ↔ Body Battery, resting HR ↔ training load).
- Day view (metrics with samples: heart rate, stress, Body Battery, HRV values, SpO2, respiration,
  steps as a counter): ← → over the days with samples, a click on a day in the main chart loads
  that day (htmx partial `/metrics/<key>/day`), 5-minute slots, the nights shaded, stress painted
  by band (rest, low, medium, high), Body Battery and stress on a fixed 0–100 axis.
- Weekday pattern: seven bars (mean per weekday in the period).
- Daily table: `<details>` with date, watch, strap and the 28-day mean, newest first.
- Library: `src/wristpull/series.py` (`METRICS` registry, `metric_series`, `rolling_baseline`,
  `smooth`, `stats`, `weekday_pattern`, `fold_weekly`, `intraday`, `night_windows`,
  `days_with_samples`); SQL allowed there like in `detail.py`. Wave A3 moves the rolling
  baselines into `insights/baselines.py` (polars) and `series.py` calls them.

### Today
Tiles with a 14-day sparkline and a link; the day's Body Battery and stress curve as one chart on
one 0–100 axis; from A3 the readiness tile with the three main reasons.

### Sleep and activities (wave A2)
Hypnogram as an ECharts custom series with the night curves (heart rate, RMSSD, respiration, SpO2)
on one time axis, stage shares against the baseline, the sleep list as bed-to-wake bars; the
activity page with pace, cadence, power and elevation as linked charts, a zone donut, a lap chart
and the track without a map.

### Insights `/insights` (wave A3) and the AI report (wave A4)
Readiness history with the contributions, acute and chronic load, sleep regularity and debt, one
scatter per pair with the bin means and the sweet spot (or "no clear sweet spot"), tag effects,
weekday pattern, and the button that writes the AI report. The formulas, the package shape and
the test list are in the plan of 2026-11-14 and move here with wave A3.

## Waves

- **A0** concept into the repo (this file, CLAUDE.md line).
- **A1** (done 2026-11-14): `series.py` + tests, metric pages, Trends tiles, Today sparklines and
  day curve, chart kinds and zoom in `app.js`, theme toggle (`theme.js`, nav and Settings),
  `web/markdown.py` for coach answers, palette tokens, German catalog, README.
- **P2** (done 2026-11-14) parity: `/days` (period presets, table, day detail partial;
  `detail.day_detail_rows` shared with the TUI), `/files` (`web/files.py`: safe paths, listing,
  preview; download route; FIT inspect; `adb/` and `staging/` hidden), `/export` (export and
  reports as in-process jobs with download links and a Markdown preview), activity name filter and
  FIT download, sleep source filter, the phone's "More" sheet in the navigation.
- **A2** (done 2026-11-14) sleep and activities: hypnogram (ECharts custom series), the night curves
  (heart rate, RMSSD, respiration, SpO2) on the bedtime axis with linked tooltips, stage shares against
  the 28-day mean (`detail.stage_baseline`), the sleep list as bed-to-wake bars on an 18:00–12:00 axis;
  the activity page with linked HR, pace (or speed), elevation, cadence and power curves, a zone donut,
  the lap chart and the track drawn from the GPS points; hours per week stacked by sport above the list.
- **P3** (done 2026-11-14) parity: coach backend check as an in-process job on Settings, `/coach/profile`
  (textarea, placeholder count), `/coach/wizard` (the TUI's questions as a form, prefilled, `apply_wizard`),
  dry run button, dry runs and failures in the run list with tokens, cost and duration, the EN/DE switch
  (cookie `wristpull_lang`, evaluated before the config and the browser language).
- **A3** (done 2026-11-14) `src/wristpull/insights/`: `baselines.py` (polars rolling mean/std/median/z,
  trailing, the day excluded, min 7 values; `rolling_stats` also serves the metric pages), `readiness.py`
  (weights hrv .25, resting HR .20, sleep .15, sleep score .15, Body Battery .10, yesterday's stress .10,
  ACWR .05; z clamped at ±2.5, 20 points per z, sleep under 6 h capped at 30, ACWR free up to 1.3 and zero
  at 1.8; contributions add up to score − 50; null under two components), `sleep.py` (one main night per
  date, bedtime as minutes since 18:00, spread and debt over 14 days against `[coach] sleep_target_h`),
  `correlate.py` (Spearman over z-scores, lags 0–2, min 20 pairs, fixed sleep bins, one-hour bedtime bins,
  rest-day plus tertile bins for load, sweet spot = best bin with ≥ 5 days that beats its neighbours by
  ≥ 0.25 z, tag effects at lag 1), `tags.py` + `store.py` (table `day_tags`, schema v8), `package.py`
  (`build_insights`). Pages: `/insights` (readiness ring and components, ACWR, bedtime/wake-up, one
  scatter per pair with bin means, tags for the last seven days, caveats), readiness tile and today's tags on
  Today; CLI `wristpull coach tag DATE +sauna -alcohol`. On the real database the package takes ~0.2 s.
- **A4** (done 2026-11-14) coach kind `insights`: `context.build_insights_package` renders the package into
  sections (readiness with components, relationships with r/n/strength/lags/bins/sweet spot, tag effects,
  sleep regularity, weekday means, previous coaching, caveats), `prompts.SECTIONS/TASKS["insights"]` (en/de:
  what the data shows / what it means / one thing to try / numbers used), the system prompt forbids
  recomputing; model `[coach] insights_model` → `weekly_model` → `model`; CLI `wristpull coach insights
  [--weeks N]`, button and last report on `/insights` (with a hint when newer days exist), also on `/coach`.
  `/report?kind=week|month&date=` shows the CLI's weekly/monthly Markdown with the period's charts.
- **P4** (done 2026-11-14) parity: the Workouts page ticks files and pushes them through the agent
  (`workout_push` with dry run, force and schedule; `workout_push_upcoming`), verifies (`workout_verify`),
  deletes a file after a confirmation step (`workouts.store.delete`, never outside the folder), shows the
  push history; `/workouts/watch` reads the watch (`watch_list`), lists active and pending workouts with
  the limit warning and removes one after a confirmation (`watch_remove`, backup first). The job partial
  polls `/sync/job` like the Sync page.
- **A5/P5** (done 2026-11-14) `/help` (page overview with TUI keys and command lines, host paths, docs),
  the phone's "more" menu (since P2), `tests/test_web_parity.py` (every TUI screen and agent job kind has
  a web counterpart; the help page lists every screen), web screenshots from synthetic data in
  `docs/screenshots/web-*.jpg`, the README table "TUI key ↔ web page", the setup script's closing note on
  the agent and the start password.

Verification per wave: `uv run pyflakes src tests && uv run pytest -q`, the boundary and catalog
tests, `docker compose build web && docker compose up -d web`, and a look at the pages on the
laptop and the phone (375 px: no horizontal scroll, chips scrollable, charts usable, dark mode).
