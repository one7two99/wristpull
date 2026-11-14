# wristpull training coach – concept

Status: concept approved 2026-11-01; waves C1 to C4 implemented the same day (`wristpull coach daily|weekly|ask|show|rate|profile [--wizard]|note|notes|plan-ics`; Ollama, Claude SDK and OpenRouter backends; number check; TUI screen 9 with goal wizard and journal; plan adherence; systemd/udev templates under `contrib/`). Implementation waves at the end.

## Why

wristpull collects, normalises and merges wearable data locally. The next step of the
[project vision](../README.md#roadmap) is to turn that data into training guidance: a short
morning check ("how recovered am I, what should I do today?") and a weekly review with a plan
for the next week, produced by an LLM of the user's choice. Decisions taken with the user:

- Backends: **Claude API**, **Ollama** (local), **OpenRouter**. The backend choice is a privacy
  choice and is made explicitly.
- Training goals are chosen in a **multiple-choice wizard** that generates an **editable prompt
  with placeholders**; the user may rewrite it freely.
- Rhythm: **daily short check + weekly detailed review**, plus free questions on demand.
- Memory: every run and the user's feedback are stored **locally in the wristpull SQLite**, so
  later runs stay consistent with earlier advice.

Machine used for development: 4-core laptop CPU without GPU, 15 GB RAM, Ollama with
`gemma3:12b` (about 3 tokens/s), `gemma3:4b`, `qwen3.5:4b`.

## Guiding principles

- **Compute deterministically, let the model interpret.** Deltas, baselines, acute:chronic
  workload ratio (ACWR) and recovery flags are computed in Python. The model receives finished
  numbers with units and must list the numbers it used under a fixed heading; a check compares
  them with the data package and warns about invented values.
- **No raw data upload.** Only daily and weekly aggregates and activity summaries. Never
  per-second records, GPS, serial numbers. `--dry-run` writes the exact package to a file.
- **Backend choice is a privacy choice.** Ollama keeps everything on the machine. Claude API
  and OpenRouter send the package to the provider; before the first run per backend the UI
  shows a note with the package size.
- **No medical advice.** No diagnoses; on warning signs (high resting HR, low HRV, SpO2 below
  90 %) the coach stays conservative and points to a doctor. Recommendations are training
  suggestions.
- **Everything in the TUI also works from the CLI** (`wristpull coach …`) so a timer can run
  the morning check.
- **Missing is not zero.** Gaps (for example the Garmin Connect sync race) are named as gaps.

## Module layout `src/wristpull/coach/`

```
_http.py      post_json(url, body, headers, timeout) via urllib; retries with backoff on 429/5xx (Retry-After honoured)
backends.py   ChatRequest/ChatResponse; Protocol Backend.complete(req, log) + check()
              ClaudeBackend     – official anthropic SDK, optional extra `coach` (uv sync --extra coach)
              OllamaBackend     – POST /api/chat, stdlib; always sets num_ctx (the default 4096 truncates silently), think=false
              OpenRouterBackend – OpenAI-compatible /api/v1/chat/completions, stdlib, usage.include for costs
              FakeBackend       – tests; make_backend(config)
metrics.py    acwr(), monotony(), baselines(), recovery_flags() from daily_frame / hrv
context.py    DataPackage; build_daily / build_weekly / build_ask; render(pkg, budget_tokens) trims in a fixed order
prompts.py    wizard QUESTIONS + text fragments, system_prompt(kind, lang), user_prompt(...),
              SECTIONS per language, parse_sections(answer), verify_numbers(answer, package)
profile.py    <data_dir>/coach/profile.md (the editable prompt) + profile.toml (answers), marker merge, placeholder check
store.py      coach_runs, coach_feedback; save_run, recent_runs, add_feedback, memory_digest(kind, max_tokens)
runner.py     run_coach(conn, config, kind, *, today, language, question, backend, dry_run, log) -> CoachResult
```

SQL lives only in `store.py` and `context.py` (library code). CLI:
`wristpull coach daily|weekly|ask "…"|profile [--wizard]|show [--last N]|rate RUN -1|0|+1 [--note]`
with `--backend --model --dry-run --lang --force`. TUI: screen 9 "Coach" as the ninth tab, running
through the existing JobRunner.

## Data package

Sources: `daily_frame`, `weekly_frame`, `activities_frame`, `sleep_frame`, `period_overview`,
the `hrv` status row, VO2max from `metrics`; formatted with the helpers of `export.py`.

**Daily (2.5–3.5k tokens):** header with date and coverage ("Garmin data on 6 of 7 days"); last
night (bed/wake, duration, stages, score, HRV of the night vs 7- and 28-day means, Garmin HRV
status); yesterday (steps, resting HR, stress, body battery, intensity minutes, activities with
duration/HR/load); 7-day table (date, steps, RHR, HRV, stress, BB max, sleep h, load);
precomputed deltas ("RHR 52, 7d 54, 28d 55, Δ28d −3"); load (acute, chronic, ACWR, monotony,
"insufficient history" below 28 days); **flags** (HRV more than 10 % under baseline, RHR ≥ +5,
sleep < 6 h or score < 60, BB max < 50, ACWR > 1.3); journal line; digest of recent advice with
ratings; data caveats (unverified mappings, hours missing because another app synced first).

**Weekly (4–5.5k tokens):** week vs previous week with deltas, 7-day table, activities (max 15,
zone minutes), nights, 6-week trend (RHR, HRV, sleep, load), ACWR trend, last week's plan plus
the seven daily suggestions one line each with feedback, caveats.

**Ask:** the daily package plus the question. Budget: 6000 tokens for local models, 12000 for
Claude/OpenRouter (configurable); `render` trims activities → nights → weeks → digest.

## Prompt design

- System prompt in English with "Answer in German/English" following the TUI language
  (`--lang` overrides): a sober endurance and fitness coach, uses only the aggregates provided,
  profile constraints are hard, conservative when flags are set (reduce volume, propose rest),
  names missing values as missing, every number comes from the data and is repeated under
  "Numbers used", the daily suggestion is about 150 words.
- **Output contract** with fixed H2 headings (per language in `SECTIONS`, the parser knows both):
  - daily: Status · Today's suggestion · Watch out for · Numbers used
  - weekly: Week in review · Recovery · Training load · Plan for next week (table Mon–Sun) · Questions for you · Numbers used
  - ask: Answer · Numbers used
- User prompt: `# Goal profile` → `# Data` → `# Previous coaching` → `# Task` → `# Output format`.
- **Claude**: default model `claude-opus-5-5` (`claude-sonnet-5-5` and `claude-haiku-5-5`
  configurable), adaptive thinking implicit, `output_config.effort` "medium", `max_tokens` 2000,
  no temperature parameter (rejected by current models), `fallbacks: "default"`; SDK retries and
  typed errors. `stop_reason == "refusal"` is reported as an error, `max_tokens` as "truncated".
- **Ollama**: `gemma3:4b` for the daily check (fast), `gemma3:12b` for the weekly review (about
  5 minutes on this CPU), `num_ctx` 8192, timeout 600 s; the job runs in the worker thread and can
  be cancelled before sending.
- **OpenRouter**: any model (`anthropic/claude-sonnet-4.6`, open models), cost from `usage.cost`.
- No streaming in version 1; the log reports "sending N tokens to backend/model" and the duration.

## Goal wizard (multiple choice → prompt)

Questions: goals (multi: endurance/running, fitness & health, strength/body composition, event);
for an event its kind, date `{{event_date}}`, target time `{{target_time}}`; time budget (hours
per week, sessions, fixed days); experience; style (cautious/balanced/ambitious); preferences
(outdoors, treadmill, bike, strength at home/gym); constraints `{{constraints}}`; optional age,
weight, max HR; tone (terse/detailed). Each answer maps to text fragments
(`prompts.FRAGMENTS[key][option][lang]`). Result:

```
<!-- wristpull:wizard:start sha256=… -->
… generated part with {{placeholders}} …
<!-- wristpull:wizard:end -->
## My notes
(free text, survives a re-run of the wizard)
```

Answers are kept in `profile.toml` to pre-fill the wizard. Unfilled placeholders: `coach profile`
warns, `run_coach` refuses without `--force`. TUI: wizard as a modal with checkboxes/radios,
prompt editing via `$EDITOR` (`app.suspend()`).

## Storage

```sql
coach_runs(id, kind, period_start, period_end, language, backend, model, profile_sha256,
  prompt_sha256, system_prompt, user_prompt, question, answer_md, input_tokens, output_tokens,
  tokens_estimated, cost_usd, duration_s, stop_reason, truncated, status 'ok|error|dry_run',
  error, created_at_utc)
coach_feedback(id, run_id REFERENCES coach_runs ON DELETE CASCADE, rating -1|0|+1, note, created_at_utc)
journal(local_date PRIMARY KEY, note, created_at_utc)
```

Prompts are stored in full (local database, reproducibility). `SCHEMA_VERSION` becomes 2. Digest:
the last 3 daily and 2 weekly answers reduced to "suggestion"/"plan" plus rating, fixed cap, oldest
dropped first. Costs from `[coach.prices]` (USD per million tokens per model, defaults in code,
Ollama 0); tokens from the response, otherwise estimated and flagged.

## Configuration

```toml
[coach]
backend = "ollama"                  # ollama | claude | openrouter
model = "gemma3:4b"
weekly_model = "gemma3:12b"         # optional, defaults to model
api_key_env = ""                    # default ANTHROPIC_API_KEY or OPENROUTER_API_KEY; never the key itself
ollama_url = "http://localhost:11434"
max_tokens = 2000
timeout_s = 600
token_budget = 6000
effort = "medium"                   # Claude only
language = ""                       # empty = TUI language
[coach.prices]
"claude-opus-5-5" = { input = 4.0, output = 20.0 }
```

`CoachConfig` as a nested dataclass `Config.coach`; `validate_config` checks backend, model, numbers.

## Risks

- Small local models invent numbers and break the format: precompute deltas, `verify_numbers`
  warns ("3 numbers not in the data"), `parse_sections` is tolerant with a fallback section.
- CPU runtime of the 12b model: weekly only, worker thread, cancellable.
- Data gaps from foreign syncs distort recovery: coverage and caveats in the package.
- Rate limits and network errors: retries, then plain-text error in the job log; no silent retry.

## Further ideas noted along the way

Training load (ACWR, monotony) and a deterministic recovery index feed the coach; sleep
consistency (spread of bedtimes over 14 days); a source-difference alert (Garmin vs strap steps or
resting HR); a one-line daily `journal`; the weekly plan as an ICS calendar file.

## Implementation waves

- **C1 (one session):** `_http.py`, `backends.py` with Ollama + Fake, `metrics.py`, daily
  `context.py`, `prompts.py` with a default profile template (placeholders), `store.py` + schema,
  `runner.py` daily/ask/dry-run, `[coach]` config, CLI `coach daily|ask|show`. Tests with the
  FakeBackend (context, budget trimming, store, runner). First real run with `gemma3:4b`.
- **C2:** weekly context, digest, ClaudeBackend (extra `coach`), OpenRouterBackend with fake
  transport tests, price table, `verify_numbers`, `coach weekly|rate`.
- **C3:** goal wizard (`profile.py`, `coach profile --wizard`, marker merge), TUI screen 9 with
  runs table left, answer as Markdown right, rating, wizard modal, prompt editor; mockup
  catalog, README.
- **C4:** journal, systemd timer for the morning run, plan adherence from the activities, ICS
  export, opt-in live test (skipped without an API key).
- **C5 (2026-11-02): closing the loop onto the watch.** The weekly plan becomes structured
  workouts: `workouts/drafts.py` turns each non-rest row into a Markdown file (warm-up, main part
  by time or distance with the target named in the plan or guessed from its intensity words,
  cool-down), `fit/workout.py` encodes them as FIT workout files (plus an optional schedule file)
  and `workouts/push.py` copies them to `GARMIN/NewFiles/`; `verify` reads `GARMIN/Workouts/` to
  confirm what the watch took over. CLI `wristpull workout …`, TUI screen 0, key `t` on the coach
  screen. The push history (`workout_pushes`) is the next candidate for the weekly adherence
  section: planned workout → pushed → recorded activity.
- **C6 (2026-11-14): the insights report.** A fourth run kind `insights` narrates the deterministic
  statistics of `src/wristpull/insights/` (readiness, sleep regularity, lagged correlations, sweet spots,
  tag effects; see `docs/analysis-concept.md`). The package is rendered into sections by
  `context.build_insights_package`; the model may interpret but not recompute, keeps the strength labels
  and proposes one testable change. Model: `[coach] insights_model`, else `weekly_model`, else `model`.
