-- wristpull schema. All timestamps are ISO-8601 UTC strings ("2026-10-31T06:12:33Z");
-- tz holds the IANA zone used to derive local_date.
-- source is one of: garmin_usb | gadgetbridge_garmin | garmin_connect (account export) | gadgetbridge_zepp
-- raw_files.kind: activity | monitor | sleep | metrics | hrv | other (FIT), database (Gadgetbridge), uds | sleep_json | activities_json | maxmet | biometrics (Connect export)

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_files (
    id                   INTEGER PRIMARY KEY,
    sha256               TEXT NOT NULL UNIQUE,
    source               TEXT NOT NULL,
    kind                 TEXT NOT NULL,              -- activity | monitor | sleep | metrics | hrv | other
    original_name        TEXT NOT NULL,
    origin_path          TEXT,                       -- where the file was found (device path)
    archive_path         TEXT NOT NULL,              -- relative to the raw directory
    size_bytes           INTEGER NOT NULL,
    device_mtime_utc     TEXT,
    imported_at_utc      TEXT NOT NULL,
    parsed_at_utc        TEXT,
    parse_status         TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error
    parse_error          TEXT,
    fit_file_type        TEXT,
    fit_time_created_utc TEXT,
    device_serial        TEXT,
    device_product       TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER PRIMARY KEY,
    source               TEXT NOT NULL,
    raw_file_id          INTEGER REFERENCES raw_files(id),
    start_utc            TEXT NOT NULL,
    end_utc              TEXT,
    tz                   TEXT NOT NULL,
    local_date           TEXT NOT NULL,
    sport                TEXT,
    sub_sport            TEXT,
    profile_name         TEXT,
    name                 TEXT,                   -- the activity's name in Garmin Connect (account export), if known
    elapsed_s            REAL,
    timer_s              REAL,
    distance_m           REAL,
    calories             INTEGER,
    avg_hr               INTEGER,
    max_hr               INTEGER,
    min_hr               INTEGER,
    avg_speed_mps        REAL,
    max_speed_mps        REAL,
    ascent_m             REAL,
    descent_m            REAL,
    avg_cadence          REAL,
    max_cadence          REAL,
    avg_power_w          REAL,
    max_power_w          REAL,
    normalized_power_w   REAL,
    aerobic_te           REAL,
    anaerobic_te         REAL,
    training_load        REAL,
    training_stress_score REAL,
    avg_temperature_c    REAL,
    avg_respiration      REAL,
    num_laps             INTEGER,
    hr_zone_times_s      TEXT,                       -- JSON list, seconds per HR zone
    device_serial        TEXT,
    UNIQUE (source, start_utc, sport)
);

CREATE TABLE IF NOT EXISTS activity_records (
    id            INTEGER PRIMARY KEY,
    activity_id   INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    ts_utc        TEXT NOT NULL,
    hr            INTEGER,
    distance_m    REAL,
    speed_mps     REAL,
    cadence       REAL,
    altitude_m    REAL,
    lat           REAL,
    lon           REAL,
    power_w       REAL,
    temperature_c REAL,
    respiration   REAL,
    UNIQUE (activity_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS activity_laps (
    id            INTEGER PRIMARY KEY,
    activity_id   INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    lap_index     INTEGER NOT NULL,
    start_utc     TEXT,
    end_utc       TEXT,
    elapsed_s     REAL,
    timer_s       REAL,
    distance_m    REAL,
    avg_hr        INTEGER,
    max_hr        INTEGER,
    avg_speed_mps REAL,
    calories      INTEGER,
    ascent_m      REAL,
    descent_m     REAL,
    UNIQUE (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS heart_rate (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    bpm         INTEGER NOT NULL,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_heart_rate_day ON heart_rate (source, local_date);

CREATE TABLE IF NOT EXISTS resting_hr (
    id                     INTEGER PRIMARY KEY,
    source                 TEXT NOT NULL,
    ts_utc                 TEXT NOT NULL,
    tz                     TEXT NOT NULL,
    local_date             TEXT NOT NULL,
    resting_hr             INTEGER,
    current_day_resting_hr INTEGER,
    raw_file_id            INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

-- Cumulative daily counters per activity type, as the watch reports them (typically every 15 min).
CREATE TABLE IF NOT EXISTS steps (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    tz              TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    activity_type   TEXT NOT NULL,
    steps           INTEGER,
    distance_m      REAL,
    active_time_s   REAL,
    active_calories INTEGER,
    raw_file_id     INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc, activity_type)
);
CREATE INDEX IF NOT EXISTS idx_steps_day ON steps (source, local_date);

CREATE TABLE IF NOT EXISTS intensity_minutes (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    moderate    INTEGER NOT NULL DEFAULT 0,
    vigorous    INTEGER NOT NULL DEFAULT 0,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

-- Garmin stress 0..100; negative values are kept as reported (-1 unmeasurable, -2 during activity).
CREATE TABLE IF NOT EXISTS stress (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    level       INTEGER NOT NULL,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_stress_day ON stress (source, local_date);

CREATE TABLE IF NOT EXISTS body_battery (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    level       INTEGER NOT NULL,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_body_battery_day ON body_battery (source, local_date);

CREATE TABLE IF NOT EXISTS sleep (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    raw_file_id     INTEGER REFERENCES raw_files(id),
    start_utc       TEXT NOT NULL,
    end_utc         TEXT NOT NULL,
    tz              TEXT NOT NULL,
    local_date      TEXT NOT NULL,              -- local date of waking up
    duration_s      INTEGER,
    deep_s          INTEGER,
    light_s         INTEGER,
    rem_s           INTEGER,
    awake_s         INTEGER,
    unmeasurable_s  INTEGER,
    score           INTEGER,
    quality_score   INTEGER,
    recovery_score  INTEGER,
    awakenings      INTEGER,
    avg_stress      REAL,
    UNIQUE (source, start_utc)
);

CREATE TABLE IF NOT EXISTS sleep_stages (
    id        INTEGER PRIMARY KEY,
    sleep_id  INTEGER NOT NULL REFERENCES sleep(id) ON DELETE CASCADE,
    start_utc TEXT NOT NULL,
    end_utc   TEXT NOT NULL,
    stage     TEXT NOT NULL,                     -- awake | light | deep | rem | unmeasurable
    UNIQUE (sleep_id, start_utc)
);

CREATE TABLE IF NOT EXISTS hrv (
    id                         INTEGER PRIMARY KEY,
    source                     TEXT NOT NULL,
    ts_utc                     TEXT NOT NULL,
    tz                         TEXT NOT NULL,
    local_date                 TEXT NOT NULL,
    weekly_avg_ms              REAL,
    last_night_avg_ms          REAL,
    last_night_5min_high_ms    REAL,
    baseline_low_upper_ms      REAL,
    baseline_balanced_lower_ms REAL,
    baseline_balanced_upper_ms REAL,
    status                     TEXT,
    raw_file_id                INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

CREATE TABLE IF NOT EXISTS hrv_values (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    rmssd_ms    REAL NOT NULL,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

CREATE TABLE IF NOT EXISTS spo2 (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    spo2_pct    INTEGER NOT NULL,
    confidence  INTEGER,
    mode        TEXT,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

CREATE TABLE IF NOT EXISTS respiration (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    tz              TEXT NOT NULL,
    local_date      TEXT NOT NULL,
    breaths_per_min REAL NOT NULL,
    raw_file_id     INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

-- Skin / wrist temperature (Zepp OS straps, Garmin wrist temperature).
CREATE TABLE IF NOT EXISTS temperature (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    celsius     REAL NOT NULL,
    kind        TEXT,                        -- skin | body | ambient | unknown
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc)
);

-- Sparse fitness metrics (vo2_max, ...).
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    sport       TEXT,
    raw_file_id INTEGER REFERENCES raw_files(id),
    UNIQUE (source, ts_utc, metric, sport)
);

-- Derived per local day, rebuilt from the sample tables after every import.
-- Daily values the Garmin Connect account export delivers as aggregates (UDS files): no time series,
-- so they stay out of the sample tables; rebuild_daily_summary overlays them on NULL columns only.
CREATE TABLE IF NOT EXISTS connect_daily (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    raw_file_id         INTEGER REFERENCES raw_files(id),
    local_date          TEXT NOT NULL,
    tz                  TEXT NOT NULL,
    steps               INTEGER,
    distance_m          REAL,
    active_calories     INTEGER,
    active_time_s       REAL,
    moderate_min        INTEGER,
    vigorous_min        INTEGER,
    hr_min              INTEGER,
    hr_max              INTEGER,
    resting_hr          INTEGER,
    stress_avg          REAL,
    stress_max          INTEGER,
    body_battery_min    INTEGER,
    body_battery_max    INTEGER,
    spo2_avg            REAL,
    spo2_min            INTEGER,
    respiration_avg     REAL,
    UNIQUE (source, local_date)
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id                      INTEGER PRIMARY KEY,
    source                  TEXT NOT NULL,
    local_date              TEXT NOT NULL,
    tz                      TEXT NOT NULL,
    steps                   INTEGER,
    distance_m              REAL,
    active_calories         INTEGER,
    active_time_s           REAL,
    moderate_min            INTEGER,
    vigorous_min            INTEGER,
    hr_min                  INTEGER,
    hr_max                  INTEGER,
    hr_avg                  REAL,
    resting_hr              INTEGER,
    stress_avg              REAL,
    stress_max              INTEGER,
    body_battery_min        INTEGER,
    body_battery_max        INTEGER,
    spo2_avg                REAL,
    spo2_min                INTEGER,
    respiration_avg         REAL,
    temperature_avg         REAL,
    sleep_duration_s        INTEGER,
    sleep_score             INTEGER,
    hrv_last_night_avg_ms   REAL,
    activities_count        INTEGER,
    activities_duration_s   REAL,
    activities_distance_m   REAL,
    training_load           REAL,
    updated_at_utc          TEXT NOT NULL,
    UNIQUE (source, local_date)
);

-- Training coach: every LLM run with its prompts and answer, the user's feedback, a daily journal line.
CREATE TABLE IF NOT EXISTS coach_runs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,               -- daily | weekly | ask
    period_start    TEXT NOT NULL,               -- local dates covered by the data package
    period_end      TEXT NOT NULL,
    language        TEXT NOT NULL,
    backend         TEXT NOT NULL,
    model           TEXT NOT NULL,
    profile_sha256  TEXT,
    prompt_sha256   TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    user_prompt     TEXT NOT NULL,
    question        TEXT,
    answer_md       TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    tokens_estimated INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,
    duration_s      REAL,
    stop_reason     TEXT,
    truncated       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok',  -- ok | error | dry_run
    error           TEXT,
    created_at_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_feedback (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES coach_runs(id) ON DELETE CASCADE,
    rating          INTEGER NOT NULL CHECK (rating BETWEEN -1 AND 1),
    note            TEXT,
    created_at_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    local_date      TEXT PRIMARY KEY,
    note            TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL
);

-- Workouts pushed to the watch as FIT files (the Markdown sources live under <data_dir>/workouts/).
CREATE TABLE IF NOT EXISTS workout_pushes (
    id               INTEGER PRIMARY KEY,
    path             TEXT,                       -- Markdown file the FIT was built from
    name             TEXT NOT NULL,              -- wkt_name as written into the file
    local_date       TEXT,                       -- planned day
    sha256           TEXT NOT NULL,              -- of the FIT bytes
    time_created_utc TEXT NOT NULL,              -- file_id.time_created (key for schedule files and verification)
    target           TEXT NOT NULL,              -- where the file was written
    watch_serial     TEXT,
    status           TEXT NOT NULL,              -- dry_run | pushed | verified
    pushed_at_utc    TEXT NOT NULL,
    verified_at_utc  TEXT
);

-- Where each Bluetooth fetch kind of a band left off (source zepp_ble): the next pull asks the band
-- for everything after last_utc. Cleared by `wristpull strap pair`.
CREATE TABLE IF NOT EXISTS strap_sync (
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    last_utc        TEXT NOT NULL,
    updated_at_utc  TEXT NOT NULL,
    PRIMARY KEY (source, kind)
);

-- Jobs the web dashboard hands to the device agent (`wristpull agent`, Compose service `agent`): the only
-- way the network-reachable `web` container reaches the watch, the phone and the band. One job runs at a
-- time; `stop_requested` is the cooperative stop flag the running import polls between files.
CREATE TABLE IF NOT EXISTS agent_jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,              -- import_garmin | pull_phone | pull_strap | workout_* | watch_*
    params          TEXT NOT NULL,              -- JSON, validated against agent.kinds before it is stored
    title           TEXT NOT NULL,              -- msgid shown by TUI and web
    cli             TEXT NOT NULL,              -- the equivalent command line
    status          TEXT NOT NULL,              -- queued | running | done | error | stopped
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    requested_utc   TEXT NOT NULL,
    started_utc     TEXT,
    finished_utc    TEXT,
    result          TEXT,                       -- JSON of the result dataclass (jobtext.jsonable)
    error           TEXT
);

CREATE TABLE IF NOT EXISTS agent_job_log (
    job_id          INTEGER NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    line            TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);

-- What the agent last reported: heartbeat, watch/phone attached (from sysfs), its version.
CREATE TABLE IF NOT EXISTS agent_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);
