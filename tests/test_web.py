"""Web dashboard: auth primitives, pages on synthetic data, CSRF, rate limit, serve, boundary and catalog."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from conftest import build_monitor_fit  # noqa: E402
from wristpull.coach.store import save_run  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.importers.garmin import import_garmin  # noqa: E402
from wristpull.web import pages  # noqa: E402
from wristpull.web.app import WebDeps, create_app, format_delta, format_duration, format_hm, format_km, format_pace, format_value, local_time  # noqa: E402
from wristpull.web.auth import RateLimiter, SessionCodec, hash_password, new_session, valid_hash, verify_password  # noqa: E402
from wristpull.web.i18n import pick_language  # noqa: E402
from wristpull.web.server import PASSWORD_ENV, SECRET_ENV, prompt_hash, resolve_secret, serve  # noqa: E402

PASSWORD = "correct horse battery"
SECRET = "s" * 32
TODAY = date(2026, 10, 7)
WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "wristpull" / "web"


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def seeded(config: Config, conn, garmin_tree: Path) -> Config:
    for i in range(5):  # five more days of monitoring data before the fixture's 2026-10-06 (local midnight = 22:00 UTC)
        start = datetime(2026, 9, 30, 22, 0, tzinfo=UTC) + timedelta(days=i)
        (garmin_tree / "Monitor" / f"M6A2{i:04d}.FIT").write_bytes(build_monitor_fit(start))
    import_garmin(conn, config, path=garmin_tree)
    save_run(conn, kind="daily", period_start="2026-10-06", period_end="2026-10-06", language="en", backend="fake", model="m1",
             prompt_sha256="x", system_prompt="s", user_prompt="u", answer_md="## Status\nWell recovered <b>today</b>.", status="ok")
    return config


def client_for(config: Config, password_hash: str | None, limiter: RateLimiter | None = None, **deps) -> TestClient:
    from wristpull.web.jobs import JobRegistry

    deps.setdefault("jobs", JobRegistry(config.db_path, threaded=False))
    deps.setdefault("env", {})  # the settings page shows .env values; tests pass their own
    app = create_app(config, password_hash=password_hash, secret=SECRET, deps=WebDeps(today=lambda: TODAY, limiter=limiter or RateLimiter(), **deps))
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client: TestClient, password: str = PASSWORD):
    page = client.get("/login")
    return client.post("/login", data={"csrf": csrf_of(page.text), "password": password})


# ---- auth primitives --------------------------------------------------------------------


def test_password_hash_roundtrip():
    encoded = hash_password("pw", salt=b"0" * 16, n=2**10)
    assert encoded.startswith("scrypt:1024:8:1:") and "$" not in encoded and valid_hash(encoded)
    assert verify_password("pw", encoded) and not verify_password("PW", encoded)
    assert not verify_password("pw", "garbage") and not verify_password("pw", "bcrypt:1:2:3:x:y")
    assert not valid_hash(None) and not valid_hash("scrypt:x:8:1:a:b")
    with pytest.raises(ValueError):
        hash_password("")


def test_session_codec_signs_and_expires():
    now = [1000.0]
    codec = SessionCodec(SECRET, max_age_s=60, clock=lambda: now[0])
    cookie = codec.dumps({"user": True, "csrf": "t"})
    assert codec.loads(cookie)["user"] is True
    payload, sig = cookie.rsplit(".", 1)
    assert codec.loads(payload + "." + sig[::-1]) is None  # tampered
    assert SessionCodec("t" * 32).loads(cookie) is None  # other secret
    assert codec.loads(None) is None and codec.loads("nodot") is None
    now[0] += 61
    assert codec.loads(cookie) is None  # expired
    with pytest.raises(ValueError):
        SessionCodec("short")
    assert new_session()["user"] is False and len(new_session()["csrf"]) > 20


def test_rate_limiter_per_client_and_overall():
    now = [0.0]
    limiter = RateLimiter(per_client=2, overall=3, window_s=10, clock=lambda: now[0])
    limiter.failure("a")
    assert not limiter.blocked("a")
    limiter.failure("a")
    assert limiter.blocked("a") and not limiter.blocked("b")
    limiter.failure("b")
    assert limiter.blocked("c")  # three failures overall block everyone
    now[0] = 11
    assert not limiter.blocked("a") and not limiter.blocked("c")
    limiter.failure("a")
    limiter.success("a")
    assert not limiter.blocked("a")


def test_language_and_formatting():
    assert pick_language("de", "en") == "de"
    assert pick_language(None, "fr;q=0.9, de-DE;q=0.8, en;q=0.5") == "de"
    assert pick_language(None, "fr, it") == "en" and pick_language(None, None) == "en" and pick_language(None, "de;q=x") == "de"
    assert format_value(11380, "int", "de") == "11.380" and format_value(11380, "int", "en") == "11,380"
    assert format_value(7.2, "hm", "de") == "7:12" and format_value(None, "int", "en") == "–" and format_value(1.25, "float1", "de") == "1,2"
    assert format_delta(-2.4, "int", "de") == "−2" and format_delta(0.2, "int", "en") == "±0" and format_delta(1500, "int", "de") == "+1.500"
    assert format_delta(-0.5, "hm", "en") == "−0:30" and format_delta(None, "int", "en") == ""
    from zoneinfo import ZoneInfo

    assert local_time("2026-10-06T05:42:00Z", ZoneInfo("Europe/Berlin")) == "06.10. 07:42" and local_time(None, ZoneInfo("UTC")) == "–"


# ---- pages ------------------------------------------------------------------------------


def test_tone():
    assert pages.tone(-3, "down") == "good" and pages.tone(3, "down") == "bad" and pages.tone(3, "up") == "good"
    assert pages.tone(0.2, "up") == "neutral" and pages.tone(None, "up") == "neutral" and pages.tone(5, None) == "neutral"


def test_today_and_trends_data(seeded: Config, conn):
    today = pages.today_data(conn, TODAY)
    assert today["as_of"] == "2026-10-06" and [t["metric"] for t in today["tiles"]][:2] == ["resting_hr", "hrv_last_night_avg_ms"]
    steps = next(t for t in today["tiles"] if t["metric"] == "steps")
    assert steps["value"] is not None and steps["n_28d"] >= 1
    assert today["coach"]["backend"] == "fake" and "Well recovered" in today["coach"]["excerpt"]
    assert "garmin_usb" in today["imports"] and today["load"]["sufficient_history"] is False
    assert today["tiles"][0]["page"] == "resting_hr" and len(today["tiles"][0]["spark"]["labels"]) == 14 and today["curve"] is None  # no tz: no curve
    from zoneinfo import ZoneInfo

    with_curve = pages.today_data(conn, date(2026, 10, 6), ZoneInfo("Europe/Berlin"))
    assert with_curve["curve"]["kind"] == "intraday" and [s["name"] for s in with_curve["curve"]["series"]] == ["Body Battery", "Stress"]
    trends = pages.trends_data(conn, TODAY, 4)
    assert trends["weeks"] == 4 and trends["start"] == "2026-09-10" and len(trends["tiles"][0]["chart"]["labels"]) == 28
    keys = [t["key"] for t in trends["tiles"]]
    assert keys[:3] == ["resting_hr", "hrv", "sleep_duration"] and "weight" not in keys and "vo2max" in keys  # no scale readings: no weight tile
    rhr = trends["tiles"][0]
    assert rhr["latest"] is not None and rhr["latest_date"] == "2026-10-06" and rhr["chart"]["kind"] == "sparkline"
    acwr = next(c for c in trends["charts"] if c["id"] == "acwr")
    assert acwr["kind"] == "acwr" and all(v is None for v in acwr["series"][0]["data"])  # < 28 days history
    assert pages.trends_data(conn, TODAY, 7)["weeks"] == 4  # unknown choice falls back
    years = pages.trends_data(conn, TODAY, 104)  # beyond a year: one point per ISO week
    labels = years["tiles"][0]["chart"]["labels"]
    assert years["weeks"] == 104 and 104 <= len(labels) <= 105 and labels[0] <= years["start"]
    assert all(len(t["chart"]["series"][0]["data"]) == len(t["chart"]["labels"]) for t in years["tiles"])
    assert all(len(c["series"][0]["data"]) == len(c["labels"]) for c in years["charts"])
    assert pages.weekly_points(["2026-10-05", "2026-10-06", "2026-10-12"], [50.0, 52.0, None], "mean") == [51.0, None]
    assert pages.weekly_points(["2026-10-05", "2026-10-06", "2026-10-12"], [70.0, 68.5, 68.0], "last") == [68.5, 68.0]
    assert pages.week_labels(["2026-10-07", "2026-10-08", "2026-10-13"]) == ["2026-10-05", "2026-10-12"]
    assert pages.acwr_series([], ["2026-10-01"]) == [None]
    assert pages.coach_blocks("## Status\nGood.\nStill good.\n\n## Today\nRest") == [
        {"kind": "heading", "text": "Status"}, {"kind": "text", "text": "Good.\nStill good."},
        {"kind": "heading", "text": "Today"}, {"kind": "text", "text": "Rest"}]


# ---- HTTP -------------------------------------------------------------------------------


def test_login_flow_and_pages(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    assert client.get("/healthz").json() == {"ok": True}
    first = client.get("/")
    assert first.status_code == 303 and first.headers["location"] == "/login"
    page = client.get("/login", headers={"accept-language": "de-DE,de;q=0.9"})
    assert page.status_code == 200 and "Passwort" in page.text and "Anmelden" in page.text
    cookie = page.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "default-src 'self'" in page.headers["content-security-policy"] and page.headers["x-frame-options"] == "DENY"
    # no CSRF token: refused before the password is even checked
    assert client.post("/login", data={"password": PASSWORD}).status_code == 403
    wrong = client.post("/login", data={"csrf": csrf_of(page.text), "password": "nope"})
    assert wrong.status_code == 401 and "Wrong password" in wrong.text
    ok = login(client)
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert client.get("/login").status_code == 303  # already logged in
    home = client.get("/", headers={"accept-language": "de"})
    assert home.status_code == 200 and "Ruhepuls" in home.text and "Schritte" in home.text
    assert "Well recovered &lt;b&gt;today&lt;/b&gt;" in home.text and "<h3>Status</h3>" in home.text  # coach text is escaped
    assert 'href="/metrics/resting_hr"' in home.text and 'id="chart-spark-steps"' in home.text
    assert "Uhr (USB)" in home.text and home.headers["cache-control"] == "no-store"
    trends = client.get("/trends?weeks=12")
    assert trends.status_code == 200 and 'id="chart-spark-resting_hr"' in trends.text and 'href="/metrics/hrv?weeks=12"' in trends.text and '"labels":[' in trends.text
    assert 'id="chart-acwr"' in trends.text and 'data-theme-toggle' in trends.text and client.get("/static/theme.js").status_code == 200
    assert 'aria-current="page">12 weeks' in trends.text and client.get("/trends?weeks=x").status_code == 200
    assert client.get("/static/vendor/echarts.min.js").status_code == 200 and client.get("/static/app.css").status_code == 200
    # logout needs the CSRF token of the logged-in session
    assert client.post("/logout").status_code == 403
    token = csrf_of(client.get("/").text)
    out = client.post("/logout", headers={"x-csrf-token": token})
    assert out.status_code == 303 and client.get("/").status_code == 303


def test_login_rate_limit(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash, RateLimiter(per_client=3))
    page = client.get("/login")
    for _ in range(3):
        assert client.post("/login", data={"csrf": csrf_of(page.text), "password": "nope"}).status_code == 401
    blocked = client.post("/login", data={"csrf": csrf_of(page.text), "password": PASSWORD})
    assert blocked.status_code == 429 and "Too many failed attempts" in blocked.text


def test_empty_database_renders(config: Config, password_hash: str):
    client = client_for(config, password_hash)
    login(client)
    home = client.get("/")
    assert home.status_code == 200 and "No data yet" in home.text and "No daily check yet" in home.text
    assert client.get("/trends?weeks=52").status_code == 200


# ---- activities and sleep (D2) ------------------------------------------------------------


def test_duration_distance_pace_formats():
    assert format_duration(3725) == "1:02:05" and format_duration(2530.4) == "42:10" and format_duration(None) == "–"
    assert format_hm(27840) == "7:44" and format_hm(None) == "–"
    assert format_km(6120, "de") == "6,12 km" and format_km(0, "en") == "–"
    assert format_pace(1800, 6000) == "5:00 /km" and format_pace(1800, 50) == "–" and format_pace(None, 6000) == "–"


def test_stage_band():
    band = pages.stage_band(
        [{"start_utc": "2026-10-05T22:00:00Z", "end_utc": "2026-10-06T00:00:00Z", "stage": "deep"},
         {"start_utc": "2026-10-06T00:00:00Z", "end_utc": "2026-10-06T06:00:00Z", "stage": "light"},
         {"start_utc": "2026-10-06T06:00:00Z", "end_utc": "2026-10-06T07:00:00Z", "stage": "awake"}],  # beyond the night: clipped
        "2026-10-05T22:00:00Z", "2026-10-06T06:00:00Z")
    assert band == [{"stage": "deep", "left": 0.0, "width": 25.0}, {"stage": "light", "left": 25.0, "width": 75.0}]


def test_activity_and_sleep_data(seeded: Config, conn):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    listing = pages.activities_list(conn, TODAY, 4, tz=tz)
    assert len(listing["items"]) == 1 and listing["sports"] and listing["items"][0]["device"] == "Watch"
    act = listing["items"][0]
    assert act["duration_s"] and act["distance_m"] and act["clock"] == "08:00"
    assert pages.activities_list(conn, TODAY, 4, sport="swimming", tz=tz)["items"] == []
    page = pages.activity_page(conn, act["id"], tz)
    assert page["charts"] and page["charts"][0]["id"] == "hr" and len(page["charts"][0]["labels"]) == len(page["charts"][0]["series"][0]["data"])
    assert pages.activity_page(conn, 99999, tz) is None
    nights = pages.sleep_list(conn, TODAY, 4, tz=tz)
    assert nights["rows"][0]["date"] == "2026-10-06" and nights["rows"][0]["watch"] and nights["rows"][0]["strap"] is None
    night = pages.night_page(conn, nights["rows"][0]["watch"]["id"], tz)
    assert night["device"] == "Watch" and night["band"] and night["stage_totals"] and night["other"] is None
    assert pages.night_page(conn, 99999, tz) is None
    assert pages.activity_title({"sport": "running", "profile_name": "Run"}) == "Run"
    assert pages.activity_title({"sport": "training", "profile_name": "Training"}) == "Training"
    assert pages.activity_title({"sport": "indoor_cycling", "profile_name": None}) == "Indoor cycling"


def test_activity_and_sleep_pages(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    listing = client.get("/activities", headers={"accept-language": "de"})
    assert listing.status_code == 200 and "Aktivitäten" in listing.text and "1 Aktivität" in listing.text
    link = re.search(r'href="/activities/(\d+)"', listing.text).group(1)
    detail = client.get(f"/activities/{link}")
    assert detail.status_code == 200 and 'id="chart-hr"' in detail.text and "Time in heart-rate zones" in detail.text
    assert client.get("/activities?weeks=52&sport=running").status_code == 200
    assert client.get("/activities/99999").status_code == 404
    sleep = client.get("/sleep")
    assert sleep.status_code == 200 and "2026-10-06" in sleep.text
    night_id = re.search(r'href="/sleep/(\d+)"', sleep.text).group(1)
    night = client.get(f"/sleep/{night_id}", headers={"accept-language": "de"})
    assert night.status_code == 200 and "Schlafphasen" in night.text and 'id="chart-hypnogram"' in night.text  # the CSS band only stands in without stage intervals
    assert client.get("/sleep/99999").status_code == 404 and client.get("/sleep/abc").status_code == 404


# ---- coach, journal, workouts (D3) ---------------------------------------------------------


def test_job_registry_runs_one_job_at_a_time(config: Config, conn):
    import threading
    import time as _time

    from wristpull.web.jobs import JobRegistry

    inline = JobRegistry(config.db_path, threaded=False)
    job = inline.start("k", "t", lambda c, log: (log("step 1"), c.execute("SELECT 1").fetchone()[0])[1])
    assert job.status == "done" and job.result == 1 and job.log == ["step 1"] and inline.current() is job and inline.get(job.id) is job
    failed = inline.start("k", "t", lambda c, log: 1 / 0)
    assert failed.status == "error" and "division" in failed.error and inline.get(99) is None
    gate = threading.Event()
    threaded = JobRegistry(config.db_path)
    first = threaded.start("a", "a", lambda c, log: gate.wait(5))
    assert first.running and threaded.start("b", "b", lambda c, log: None) is None  # busy
    gate.set()
    for _ in range(100):
        if not first.running:
            break
        _time.sleep(0.02)
    assert first.status == "done" and first.seconds >= 0


def test_coach_page_run_rate_and_journal(seeded: Config, password_hash: str):
    from wristpull.coach.backends import FakeBackend
    from wristpull.coach.profile import profile_path
    from wristpull.coach.store import recent_notes, recent_runs
    from wristpull.db import connect

    profile = profile_path(seeded)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("# Goal profile\n- Goals: run\n", "utf-8")
    fake = FakeBackend(["## Status\nFine.\n## Today's suggestion\nEasy 30 min.\n"])
    client = client_for(seeded, password_hash, coach_backend=lambda cfg, kind: fake)
    login(client)
    page = client.get("/coach", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Tagescheck" in page.text and "Well recovered" in page.text
    token = csrf_of(page.text)
    assert client.post("/coach/run", data={"kind": "daily"}).status_code == 403  # no CSRF token
    assert client.post("/coach/run", data={"csrf": token, "kind": "nope"}).status_code == 400
    assert client.post("/coach/run", data={"csrf": token, "kind": "ask"}).status_code == 400  # empty question
    started = client.post("/coach/run", data={"csrf": token, "kind": "daily"}, headers={"hx-request": "true"})
    assert started.status_code == 200 and 'id="job"' in started.text and "finished" in started.text and len(fake.requests) == 1
    poll = client.get("/coach/job?poll=1")
    assert poll.headers.get("hx-refresh") == "true" and "hx-get" not in poll.text  # done: reload once, stop polling
    asked = client.post("/coach/run", data={"csrf": token, "kind": "ask", "question": "Run today?"})
    assert asked.status_code == 303 and asked.headers["location"] == "/coach"
    with connect(seeded.db_path) as conn:
        runs = recent_runs(conn, limit=5)
    assert [r["kind"] for r in runs][:2] == ["ask", "daily"] and runs[0]["question"] == "Run today?"
    page = client.get("/coach")
    assert "Easy 30 min." in page.text and "<h3>Today's suggestion</h3>" in page.text and "Run today?" in page.text
    rated = client.post("/coach/rate", data={"csrf": token, "run_id": runs[1]["id"], "rating": "1", "next": "/"})
    assert rated.status_code == 303 and rated.headers["location"] == "/"
    assert client.post("/coach/rate", data={"csrf": token, "run_id": runs[1]["id"], "rating": "5"}).status_code == 400
    assert client.post("/coach/rate", data={"csrf": token, "run_id": 9999, "rating": "1"}).status_code == 404
    assert client.post("/coach/rate", data={"csrf": token, "run_id": "x", "rating": "1"}).status_code == 400
    assert 'aria-pressed="true">+1' in client.get("/coach").text
    noted = client.post("/journal", data={"csrf": token, "note": "heavy legs"})
    assert noted.status_code == 303 and "saved=note" in noted.headers["location"]
    with connect(seeded.db_path) as conn:
        assert recent_notes(conn, 1)[0] == {"local_date": TODAY.isoformat(), "note": "heavy legs"}
    assert "heavy legs" in client.get("/coach?saved=note").text
    # a failing backend ends the job visibly
    broken = client_for(seeded, password_hash, coach_backend=lambda cfg, kind: FakeBackend(fail=RuntimeError("backend down")))
    login(broken)
    failed = broken.post("/coach/run", data={"csrf": csrf_of(broken.get("/coach").text), "kind": "daily"}, headers={"hx-request": "true"})
    assert "backend down" in failed.text


def test_workouts_pages_new_edit_check_save(seeded: Config, password_hash: str):
    from wristpull.workouts.store import workouts_dir

    client = client_for(seeded, password_hash)
    login(client)
    page = client.get("/workouts", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Trainings" in page.text and "workout push --upcoming" in page.text
    token = csrf_of(page.text)
    created = client.post("/workouts/new", data={"csrf": token, "kind": "intervals", "date": "2026-10-09"})
    assert created.status_code == 303 and created.headers["location"].startswith("/workouts/edit?file=2026-10-09-intervals")
    again = client.post("/workouts/new", data={"csrf": token, "kind": "intervals", "date": "2026-10-09"})
    assert again.headers["location"] != created.headers["location"]  # a second one gets its own file
    edit = client.get(created.headers["location"])
    assert edit.status_code == 200 and "repeat 2-3 × 6" in edit.text and "Check: ok" in edit.text
    name = created.headers["location"].split("file=")[1]
    text = (workouts_dir(seeded) / name).read_text("utf-8")
    check = client.post("/workouts/check", data={"file": name, "text": text.replace("repeat 6x", "repeat 1x")}, headers={"x-csrf-token": token})
    assert check.status_code == 200 and "repeat count 1 outside 2-99" in check.text and "needs fixing" in check.text
    saved = client.post("/workouts/save", data={"csrf": token, "file": name, "text": text.replace("repeat 6x", "repeat 8x")})
    assert saved.status_code == 303 and "saved=1" in saved.headers["location"]
    assert "repeat 8x" in (workouts_dir(seeded) / name).read_text("utf-8")
    assert "Saved." in client.get(saved.headers["location"]).text
    listing = client.get("/workouts")
    assert "Intervals 6x400 m 09.10." in listing.text
    # paths outside the folder and unknown files are refused
    assert client.post("/workouts/save", data={"csrf": token, "file": "../evil.md", "text": "x"}).status_code == 400
    assert client.post("/workouts/save", data={"csrf": token, "file": "2026-01-01-none.md", "text": "x"}).status_code == 404
    assert client.get("/workouts/edit?file=../../etc/passwd").status_code == 404
    assert client.get("/workouts/edit?file=2026-01-01-none.md").status_code == 404
    assert client.post("/workouts/check", data={"file": "../x.md", "text": ""}, headers={"x-csrf-token": token}).status_code == 400
    assert client.post("/workouts/save", data={"file": name, "text": "x"}).status_code == 403  # no CSRF token
    drafts = client.post("/workouts/drafts", data={"csrf": token})
    assert drafts.status_code == 303 and drafts.headers["location"] == "/workouts?message=noplan"
    assert "No weekly plan stored yet" in client.get("/workouts?message=noplan").text


def test_new_workout_names_and_check_lines(config: Config):
    path = pages.new_workout(config, "nonsense", date(2026, 10, 9))
    assert path.name == "2026-10-09-easy-run-40-min.md"
    second = pages.new_workout(config, "time", date(2026, 10, 9))
    assert second.name != path.name
    from wristpull.workouts.store import load

    lines = pages.check_lines(load(path))
    assert lines["valid"] and lines["steps"][0].startswith("1 warmup") and lines["day"] == "2026-10-09"


def test_unauthenticated_posts_go_to_login(config: Config, password_hash: str):
    client = client_for(config, password_hash)
    for url in ("/coach/run", "/coach/rate", "/journal", "/workouts/new", "/workouts/save", "/workouts/check", "/workouts/drafts"):
        response = client.post(url, data={})
        assert response.status_code == 303 and response.headers["location"] == "/login", url
    for url in ("/coach", "/coach/job", "/workouts", "/workouts/edit?file=x.md"):
        assert client.get(url).status_code == 303, url


# ---- serve ------------------------------------------------------------------------------


def test_serve_requires_hash_runs_one_worker_and_warns_without_secret(config: Config, password_hash: str, tmp_path: Path):
    from wristpull import secrets as store

    calls, warnings = [], []
    # a malformed hash in the environment still fails loudly; no hash at all means the start password
    assert serve(config, env={PASSWORD_ENV: "bcrypt:nope"}, run=lambda *a, **k: calls.append(k), warn=warnings.append) == 1
    assert calls == [] and "malformed" in warnings[0]
    assert serve(config, env={}, run=lambda app, **k: calls.append((app, k)), warn=warnings.append) == 0
    assert "start password 'wristpull'" in warnings[-2] and "generated one" in warnings[-1]
    assert store.load(config)[SECRET_ENV] and oct(store.path(config).stat().st_mode & 0o777) == "0o600"
    env = {PASSWORD_ENV: password_hash}
    assert serve(config, env=env, data_dir=tmp_path / "d", behind_proxy=True, run=lambda app, **k: calls.append((app, k)), warn=warnings.append) == 0
    app, kwargs = calls[-1]
    assert kwargs["workers"] == 1 and kwargs["proxy_headers"] is True and kwargs["forwarded_allow_ips"] == "*"
    assert SECRET_ENV in warnings[-1]  # a different data dir: no store there, no env secret -> generated again
    assert serve(config, env={**env, "WRISTPULL_OLLAMA_URL": "http://host.docker.internal:11434"}, run=lambda app, **k: calls.append((app, k)), warn=warnings.append) == 0
    assert calls[-1][0].state.config.coach.ollama_url == "http://host.docker.internal:11434"
    assert resolve_secret({SECRET_ENV: "x" * 20}, warnings.append) == "x" * 20
    answers = iter(["pw1", "pw1"])
    assert verify_password("pw1", prompt_hash(lambda _p: next(answers)))
    answers = iter(["pw1", "pw2"])
    assert prompt_hash(lambda _p: next(answers)) is None


def test_cli_serve_hash_password(monkeypatch, capsys, cfg_file: Path):
    from wristpull.cli import main

    answers = iter(["secret pw", "secret pw", "a", "b"])
    monkeypatch.setattr("wristpull.web.server.getpass", lambda _p: next(answers))
    assert main(["--config", str(cfg_file), "serve", "--hash-password"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(PASSWORD_ENV + "=scrypt:") and verify_password("secret pw", out.strip().split("=", 1)[1])
    assert main(["--config", str(cfg_file), "serve", "--hash-password"]) == 1  # the two entries differ
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setattr("uvicorn.run", lambda app, **k: None)  # without a hash the server starts with the start password
    assert main(["--config", str(cfg_file), "serve"]) == 0
    assert "start password" in capsys.readouterr().err


# ---- rules ------------------------------------------------------------------------------


def test_web_has_no_sql_or_fit_parsing():
    forbidden = re.compile(r"\bSELECT\b|\.execute\(|^import fitdecode|^from fitdecode", re.MULTILINE)
    offenders = [f"{p.name}:{m.group(0)}" for p in WEB_DIR.rglob("*.py") for m in forbidden.finditer(p.read_text("utf-8"))]
    assert not offenders, offenders


def test_web_strings_have_german_translations():
    from wristpull.web.i18n import translations

    de = translations("de")
    literal = re.compile(r"""\b_\(\s*["']([^"']+)["']\s*\)""")
    ids: set[str] = set()
    for path in list(WEB_DIR.rglob("*.html")) + list(WEB_DIR.rglob("*.py")):
        ids.update(literal.findall(path.read_text("utf-8")))
    ids.update(label for _m, label, unit, _k, _g in pages.TILES)
    ids.update(unit for _m, _l, unit, _k, _g in pages.TILES if unit)
    ids.update(pages.SOURCE_LABELS.values())
    ids.update(pages.DEVICE_LABELS.values())
    ids.update(pages.STAGE_ORDER)
    ids.update({"Heart rate", "Altitude", "m"})
    ids.update(pages.COACH_KINDS)
    ids.update(pages.TEMPLATE_KINDS)
    ids.update({"draft", "pushed", "verified", "removed", "running", "cycling", "walking", "strength", "cardio"})
    ids.update({"Resting HR", "HRV last night", "Sleep", "Stress", "Training per week", "Acute:chronic load", "Watch", "Strap", "Hours", "ACWR"})
    from wristpull import series

    ids.update(m.title for m in series.METRICS)
    ids.update(m.unit for m in series.METRICS if m.unit)
    ids.update(m.explain for m in series.METRICS)
    ids.update({"7-day mean", "28-day band", "Body Battery and stress today", "Weekday pattern", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})
    from wristpull.detail import DAY_DETAIL_FIELDS

    ids.update(label for label, _c, _k in DAY_DETAIL_FIELDS)
    ids.update(pages.EXPORT_TITLES.values())
    ids.update(p["label"] for p in pages.export_form(TODAY, 28)["presets"])
    ids.update(t for _k, t, _u, _d in pages.NIGHT_CURVES)
    ids.update({"Pace", "Speed", "Cadence", "Power", "Track", "Pace per lap", "Minutes per lap", "Hours per week", "Other", "Stage shares", "This night"})
    missing = sorted(i for i in ids if de.gettext(i) == i and i not in {"Trends", "Stress", "Strap", "Sport", "Body Battery", "bpm", "ms", "h", "max", "ACWR", "Score {score}", "m", "rem", "Neutral", "Journal", "cardio",
                                                                            "%", "/min", "kg", "min", "ml/kg/min", "SpO2", "VO2max",
                                                                            "Detail", "Garmin", "Export", "Export & Report", "BB", "Int.", "…", "Filter",
                                                                            "HRV (RMSSD)", "W", "km/h", "min/km", "rpm", "spm"})
    assert not missing, missing
    assert de.ngettext("{n} warning sign", "{n} warning signs", 2) == "{n} Warnzeichen"


# ---- metric pages, day curves, markdown (analysis wave A1) --------------------------------------


def test_metric_page_data(seeded: Config, conn):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    page = pages.metric_page(conn, "resting_hr", TODAY, 4, day=None, compare=None, tz=tz)
    assert page["m"].key == "resting_hr" and page["compare"] == "hrv" and page["compare_chart"]["id"] == "compare"
    chart = page["chart"]
    assert chart["id"] == "main" and len(chart["labels"]) == 28 and chart["labels"][0] == "2026-09-10"
    roles = [s["role"] for s in chart["series"]]
    assert roles == ["watch", "mean", "band_low", "band_high"] and all(len(s["data"]) == 28 for s in chart["series"])  # no strap data: no strap series
    assert all(s["role"] in ("watch", "strap") for s in page["compare_chart"]["series"])
    assert page["stats"]["latest_date"] == "2026-10-06" and page["stats"]["n"] >= 6 and page["table"][0]["date"] == "2026-10-06"
    assert page["curve"]["day"] == "2026-10-06" and page["curve"]["prev"] == "2026-10-05" and page["curve"]["next"] is None and page["curve"]["series"]
    assert page["weekday"]["labels"][0] == "Mon" and len(page["weekday"]["series"][0]["data"]) == 7 and page["weekday"]["deviation"]  # a level: differences from the mean
    deviations = [v for v in page["weekday"]["series"][0]["data"] if v is not None]
    assert deviations and abs(sum(deviations)) < 5  # around zero, not bars from zero
    earlier = pages.metric_page(conn, "resting_hr", TODAY, 4, day="2026-10-03", compare="none", tz=tz)
    assert earlier["compare"] is None and earlier["compare_chart"] is None and earlier["curve"]["day"] == "2026-10-03" and earlier["curve"]["next"] == "2026-10-04"
    sleep = pages.metric_page(conn, "sleep_duration", TODAY, 104, day=None, compare="sleep_duration", tz=tz)
    assert sleep["curve"] is None and sleep["weekly"] and sleep["compare"] == "hrv"  # comparing with itself falls back to the default
    assert not sleep["weekday"]["deviation"] and sleep["weekday"]["title"] == "Weekday pattern"  # a volume: means from zero
    assert len(sleep["chart"]["labels"]) == len(sleep["chart"]["series"][0]["data"]) and sleep["chart"]["labels"][0] <= sleep["start"]
    assert pages.metric_page(conn, "nope", TODAY, 4, day=None, compare=None, tz=tz) is None
    vo2 = pages.metric_page(conn, "vo2max", TODAY, 12, day=None, compare=None, tz=tz)
    assert vo2["chart"]["kind"] == "points" and [s["role"] for s in vo2["chart"]["series"]] == ["watch"]


def test_metric_pages_over_http(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    page = client.get("/metrics/resting_hr?weeks=4", headers={"accept-language": "de"})
    assert page.status_code == 200 and 'id="chart-main"' in page.text and 'id="chart-curve"' in page.text and "Ruhepuls" in page.text
    assert 'data-day-url="/metrics/resting_hr/day"' in page.text and "Wochentagsmuster" in page.text and 'id="chart-compare"' in page.text
    assert client.get("/metrics/nope").status_code == 404
    assert client.get("/metrics/steps?weeks=12&compare=../etc").status_code == 200  # unknown partner: the default one
    partial = client.get("/metrics/stress/day?day=2026-10-05&weeks=4")
    assert partial.status_code == 200 and 'id="intraday"' in partial.text and 'id="chart-curve"' in partial.text and '"nights":[' in partial.text
    assert client.get("/metrics/stress/day?day=nope").status_code == 404 and client.get("/metrics/sleep_duration/day?day=2026-10-05").status_code == 404
    empty = client.get("/metrics/stress/day?day=2020-01-01")
    assert empty.status_code == 200 and 'id="chart-curve"' not in empty.text
    for key in ("hrv", "sleep_duration", "sleep_score", "stress", "body_battery", "steps", "intensity_minutes", "training_load", "spo2", "respiration", "weight", "vo2max"):
        assert client.get(f"/metrics/{key}").status_code == 200, key


def test_markdown_renderer():
    from wristpull.web.markdown import render

    html = render("## Status\nWell **recovered** <b>today</b>, `x` and *calm*.\nSecond line.\n\n- one\n- two\n  wrapped\n1. first\n\n| Day | Plan |\n|---|---|\n| Mon | easy |\n\n### Numbers used\nplain")
    assert "<h3>Status</h3>" in html and "<strong>recovered</strong>" in html and "&lt;b&gt;today&lt;/b&gt;" in html and "<code>x</code>" in html and "<em>calm</em>" in html
    assert "<br>Second line" in html and "<ul><li>one</li><li>two wrapped</li></ul>" in html and "<ol><li>first</li></ol>" in html
    assert "<th>Day</th>" in html and "<td>easy</td>" in html and "<h4>Numbers used</h4>" in html and html.endswith("<p>plain</p>")
    assert render("") == "" and render("2 * 3 * 4") == "<p>2 * 3 * 4</p>"


# ---- days, files, export, filters (parity wave P2) -----------------------------------------------


def test_day_detail_rows_and_period():
    from wristpull.detail import day_detail_rows

    row = {"date": "2026-10-06", "garmin_steps": 8123, "strap_steps": 8000, "garmin_hr_min": 48, "garmin_hr_avg": 70.2, "garmin_hr_max": 150,
           "garmin_sleep_duration_s": 7.5 * 3600, "garmin_resting_hr": None, "strap_resting_hr": 52}
    cells = day_detail_rows(row)
    labels = [c["label"] for c in cells]
    assert labels[:2] == ["Steps", "HR min / avg / max"] and "Resting HR" in labels and "Temperature avg" not in labels  # only fields with a value
    assert cells[0]["garmin"] == "8,123" and cells[0]["strap"] == "8,000" and cells[1]["garmin"] == "48 / 70 / 150" and cells[1]["strap"] == "– / – / –"
    assert next(c for c in cells if c["label"] == "Sleep")["garmin"] == "7:30 h"
    assert next(c for c in cells if c["label"] == "Resting HR")["strap"] == "52"
    assert pages.day_period(TODAY, 28, start=None, end=None, preset=None) == (date(2026, 9, 10), TODAY)
    assert pages.day_period(TODAY, 28, start=None, end=None, preset="week") == (date(2026, 10, 5), date(2026, 10, 11))
    assert pages.day_period(TODAY, 28, start="2026-10-01", end="2026-10-03", preset=None) == (date(2026, 10, 1), date(2026, 10, 3))
    assert pages.day_period(TODAY, 28, start="2026-10-03", end="2026-10-01", preset=None) == (date(2026, 9, 10), TODAY)  # reversed: default
    assert pages.day_period(TODAY, 28, start="x", end="y", preset=None) == (date(2026, 9, 10), TODAY)


def test_days_page_and_detail(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    page = client.get("/days", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Tage mit Daten" in page.text and 'hx-get="/days/detail?day=2026-10-06"' in page.text and 'aria-current="true"' in page.text
    assert "Detail 2026-10-06" in page.text and "Schritte" in page.text and 'id="day-detail"' in page.text
    earlier = client.get("/days?from=2026-09-01&to=2026-09-07")
    assert earlier.status_code == 200 and "0 days with data" in earlier.text and "Pick a day" in earlier.text
    chosen = client.get("/days?from=2026-10-01&to=2026-10-06&day=2026-10-03")
    assert chosen.status_code == 200 and "Detail 2026-10-03" in chosen.text
    partial = client.get("/days/detail?day=2026-10-04")
    assert partial.status_code == 200 and partial.text.strip().startswith('<section class="card" id="day-detail"') and "Detail 2026-10-04" in partial.text
    assert client.get("/days/detail?day=2020-01-01").status_code == 404 and client.get("/days/detail?day=nope").status_code == 404
    assert client.get("/days?preset=month").status_code == 200


def test_files_helpers(tmp_path: Path):
    from wristpull.web import files

    data = tmp_path / "Health"
    (data / "raw" / "garmin_usb").mkdir(parents=True)
    (data / "adb").mkdir()
    (data / "staging").mkdir()
    (data / ".hidden").mkdir()
    (data / "raw" / "garmin_usb" / "a.fit").write_bytes(b"x" * 10)
    (data / "notes.md").write_text("# hi\nline two\n", "utf-8")
    (data / "adb" / "adbkey").write_text("secret", "utf-8")
    top = files.listing(data, None)
    assert [d["name"] for d in top["dirs"]] == ["raw"] and [f["name"] for f in top["files"]] == ["notes.md"] and top["parent"] is None and top["crumbs"] == []
    sub = files.listing(data, "raw/garmin_usb")
    assert sub["rel"] == "raw/garmin_usb" and sub["parent"] == "raw" and [c["rel"] for c in sub["crumbs"]] == ["raw", "raw/garmin_usb"]
    assert sub["files"][0]["kind"] == "fit" and sub["files"][0]["size"] == 10
    assert files.safe_path(data, "notes.md") == (data / "notes.md").resolve() and files.safe_path(data, "") == data.resolve()
    for bad in ("../", "../Health/notes.md/../..", "adb/adbkey", "adb", "staging", ".hidden", "raw/../adb/adbkey", "../../etc/passwd"):
        with pytest.raises(files.BadPath):
            files.safe_path(data, bad)
    with pytest.raises(files.BadPath):
        files.listing(data, "notes.md")  # not a folder
    assert files.preview(data / "notes.md") == ["# hi", "line two"] and files.file_kind(data / "notes.md") == "text" and files.file_kind(data / "x.bin") == "other"


def test_files_page_and_download(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    (seeded.data_dir / "adb").mkdir(exist_ok=True)
    (seeded.data_dir / "adb" / "adbkey").write_text("secret", "utf-8")
    page = client.get("/files", headers={"accept-language": "de"})
    assert page.status_code == 200 and "?dir=raw" in page.text and "adb" not in page.text.split("filelist")[1].split("</ul>")[0] and "Dateien" in page.text
    from urllib.parse import quote

    fit = next(p for p in sorted((seeded.data_dir / "raw").rglob("*")) if p.suffix.lower() == ".fit")
    folder = quote(fit.parent.relative_to(seeded.data_dir).as_posix(), safe="")
    raw = client.get(f"/files?dir={folder}")
    assert raw.status_code == 200 and fit.name in raw.text and "?dir=raw/garmin_usb" in client.get("/files?dir=raw").text
    rel = quote(fit.relative_to(seeded.data_dir).as_posix(), safe="")
    detail = client.get(f"/files?dir={folder}&file={rel}")
    assert detail.status_code == 200 and "Messages (inspect)" in detail.text and "garmin_usb" in detail.text and "Download" in detail.text and "SHA-256" in detail.text
    download = client.get(f"/files/download?path={rel}")
    assert download.status_code == 200 and download.headers["content-disposition"].startswith("attachment") and download.content[8:12] == b".FIT"
    for bad in ("adb%2Fadbkey", "..%2F..%2Fetc%2Fpasswd", "staging", "nope.fit", "raw"):
        assert client.get(f"/files/download?path={bad}").status_code == 404, bad
    assert client.get("/files?dir=adb").status_code == 404 and client.get("/files?dir=..").status_code == 404 and client.get("/files?file=adb%2Fadbkey").status_code == 404
    (seeded.data_dir / "notes.md").write_text("# Title\nsome text\n", "utf-8")
    text = client.get("/files?file=notes.md")
    assert text.status_code == 200 and "Preview" in text.text and "some text" in text.text


def test_export_page_runs_export_and_report(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    page = client.get("/export", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Export &amp; Report" in page.text and 'data-from="2026-10-05"' in page.text and "Erzeugen" in page.text
    token = csrf_of(page.text)
    assert client.post("/export/run", data={"csrf": token, "kind": "nope", "from": "2026-10-01", "to": "2026-10-06"}).status_code == 400
    assert client.post("/export/run", data={"csrf": token, "kind": "export", "from": "2026-10-06", "to": "2026-10-01"}).status_code == 400
    started = client.post("/export/run", data={"csrf": token, "kind": "export", "from": "2026-10-01", "to": "2026-10-06", "csv": "1"}, headers={"hx-request": "true"})
    assert started.status_code == 200 and "finished" in started.text and "wrote 5 files" in started.text and "wristpull export --for-claude --from 2026-10-01 --to 2026-10-06" in started.text
    poll = client.get("/export/job?poll=1")
    assert poll.headers.get("hx-refresh") == "true"
    page = client.get("/export")
    assert "summary.md" in page.text and "daily.csv" in page.text and "/files/download?path=exports/2026-10-01_2026-10-06/summary.md" in page.text and "Open in Files" in page.text
    assert '<div class="prose">' in page.text and "<h3>" in page.text  # the Markdown preview is rendered
    summary = client.get("/files/download?path=exports%2F2026-10-01_2026-10-06%2Fsummary.md")
    assert summary.status_code == 200 and summary.content.startswith(b"#")
    report = client.post("/export/run", data={"csrf": token, "kind": "week", "from": "2026-10-01", "to": "2026-10-06", "charts": "1"})
    assert report.status_code == 303
    page = client.get("/export")
    assert "report.md" in page.text and ".svg" in page.text and "wristpull report week --date 2026-10-06 --charts" in page.text
    coach = client.get("/coach")
    assert "Weekly report" not in coach.text  # the coach page shows only coach runs


def test_activity_name_filter_and_sleep_source_filter(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    all_rows = client.get("/activities?weeks=4").text.count('class="row"')
    assert all_rows >= 1
    none = client.get("/activities?weeks=4&q=zzz-no-such-name")
    assert none.status_code == 200 and 'class="row"' not in none.text and 'value="zzz-no-such-name"' in none.text
    assert client.get("/activities?weeks=4&q=run").text.count('class="row"') >= 1
    detail_id = re.search(r'href="/activities/(\d+)"', client.get("/activities?weeks=4").text).group(1)
    detail = client.get(f"/activities/{detail_id}")
    assert "Download FIT" in detail.text and "/files/download?path=raw/" in detail.text
    watch = client.get("/sleep?weeks=4&source=watch")
    assert watch.status_code == 200 and 'aria-current="page">Watch' in watch.text and "Strap" not in watch.text.split('<ul class="list">')[1]
    strap = client.get("/sleep?weeks=4&source=strap")
    assert strap.status_code == 200 and "No nights in this period." in strap.text  # the fixture has no strap nights
    assert client.get("/sleep?weeks=4&source=nope").status_code == 200


# ---- sleep and activity charts (analysis wave A2) -------------------------------------------------


def test_activity_page_charts(seeded: Config, conn):
    from zoneinfo import ZoneInfo

    activity_id = conn.execute("SELECT id FROM activities ORDER BY id LIMIT 1").fetchone()[0]
    page = pages.activity_page(conn, activity_id, ZoneInfo("Europe/Berlin"))
    ids = [c["id"] for c in page["charts"]]
    assert ids[:2] == ["hr", "pace"] and "alt" in ids and page["pace_sport"]  # the fixture is a run with speed and altitude
    pace = next(c for c in page["charts"] if c["id"] == "pace")
    assert pace["format"] == "pace" and pace["inverse"] and pace["group"] == "activity" and all(v is None or 2 < v < 20 for v in pace["series"][0]["data"])
    assert next(c for c in page["charts"] if c["id"] == "alt")["area"] is True
    assert page["track"] and page["track"]["kind"] == "track" and len(page["track"]["series"][0]["data"]) >= 2 and page["track"]["series"][0]["data"][0][1] == pytest.approx(52.52, abs=0.01)
    assert page["donut"] is None or page["donut"]["kind"] == "donut"
    assert pages._pace_min_per_km(None) is None and pages._pace_min_per_km(0.1) is None and pages._pace_min_per_km(1000 / 300) == 5.0
    weekly = pages.weekly_sport_chart([{"sport": "running", "duration_s": 3600, "date": "2026-10-06"}, {"sport": "cycling", "duration_s": 1800, "date": "2026-10-06"}], date(2026, 10, 1), date(2026, 10, 7))
    assert weekly["kind"] == "stacked" and [s["name"] for s in weekly["series"]] == ["running", "cycling"] and weekly["series"][0]["data"][-1] == 1.0
    many = pages.weekly_sport_chart([{"sport": s, "duration_s": 600 * (i + 1), "date": "2026-10-06"} for i, s in enumerate(["a", "b", "c", "d", "e"])], date(2026, 10, 1), date(2026, 10, 7))
    assert [s["name"] for s in many["series"]] == ["e", "d", "c", "Other"] and many["series"][-1]["data"][-1] == pytest.approx(0.5)  # a + b fold into Other
    assert pages.weekly_sport_chart([], date(2026, 10, 1), date(2026, 10, 7)) is None


def test_night_page_charts_and_sleep_bars(seeded: Config, conn):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    sleep_id = conn.execute("SELECT id FROM sleep ORDER BY id LIMIT 1").fetchone()[0]
    page = pages.night_page(conn, sleep_id, tz)
    assert page["hypnogram"] and page["hypnogram"]["kind"] == "hypnogram" and page["hypnogram"]["stages"] == ["deep", "light", "rem", "awake"]
    blocks = page["hypnogram"]["series"][0]["data"]
    assert blocks and all(0 <= a < b <= page["hypnogram"]["xmax"] and 0 <= k <= 3 for a, b, k in blocks) and 0 <= page["hypnogram"]["x0"] < 1440
    kinds = {c["id"]: c for c in page["curves"]}
    assert "heart_rate" in kinds and kinds["heart_rate"]["kind"] == "timeline" and kinds["heart_rate"]["series"][0]["points"][0][0] >= 0
    assert page["shares"] is None  # only one night in the fixture: no 28-day baseline yet
    assert [c["id"] for c in page["charts"]][0] == "hypnogram"
    bar = pages.night_bar("2026-10-05T21:30:00Z", "2026-10-06T05:00:00Z", tz)  # 23:30–07:00 local
    assert bar["left"] == pytest.approx(100 * 5.5 / 18, abs=0.1) and bar["width"] == pytest.approx(100 * 7.5 / 18, abs=0.1)
    assert pages.night_bar("2026-10-05T15:00:00Z", "2026-10-05T16:00:00Z", tz) == {"left": 100.0, "width": 0.5}  # an afternoon nap sits off the night axis: a sliver at its end
    assert pages.score_tone(85) == "good" and pages.score_tone(70) == "mid" and pages.score_tone(10) == "low" and pages.score_tone(None) == "none"
    rows = pages.sleep_list(conn, TODAY, 4, tz=tz)
    assert rows["n"] >= 1 and rows["mean_s"] and rows["rows"][0]["watch"]["bar"]["width"] > 0 and rows["rows"][0]["watch"]["tone"] in ("good", "mid", "low", "none")


def test_night_samples_and_stage_baseline(seeded: Config, conn):
    from wristpull.detail import night_samples, stage_baseline

    night = conn.execute("SELECT source, start_utc, end_utc, local_date FROM sleep ORDER BY id LIMIT 1").fetchone()
    samples = night_samples(conn, night["source"], night["start_utc"], night["end_utc"])
    assert "heart_rate" in samples and samples["heart_rate"][0][0] >= 0 and samples["heart_rate"] == sorted(samples["heart_rate"])
    assert stage_baseline(conn, night["source"], night["local_date"])["n"] == 0
    later = stage_baseline(conn, night["source"], "2026-11-01")
    assert later["n"] == 1 and later["deep_s"] is not None and later["duration_s"] > 0


def test_activity_night_and_sleep_pages_render_charts(seeded: Config, password_hash: str):
    client = client_for(seeded, password_hash)
    login(client)
    detail_id = re.search(r'href="/activities/(\d+)"', client.get("/activities?weeks=4").text).group(1)
    page = client.get(f"/activities/{detail_id}", headers={"accept-language": "de"})
    assert page.status_code == 200 and 'id="chart-pace"' in page.text and 'id="chart-track"' in page.text and '"group":"activity"' in page.text and "Tempo" in page.text
    listing = client.get("/activities?weeks=4")
    assert 'id="chart-weekly"' in listing.text and '"kind":"stacked"' in listing.text
    sleep_id = re.search(r'href="/sleep/(\d+)"', client.get("/sleep?weeks=4").text).group(1)
    night = client.get(f"/sleep/{sleep_id}", headers={"accept-language": "de"})
    assert night.status_code == 200 and 'id="chart-hypnogram"' in night.text and '"stage_names":[' in night.text and 'id="chart-heart_rate"' in night.text
    nights = client.get("/sleep?weeks=4")
    assert 'class="nightbar watch"' in nights.text and "18:00" in nights.text and "Ø" in nights.text


# ---- devices & sync: jobs for the device agent (parity W1) ------------------------------------


def test_sync_page_enqueues_for_the_agent_and_polls_until_done(seeded: Config, password_hash: str, tmp_path: Path, garmin_tree: Path):
    from datetime import UTC, datetime

    from fake_adb import FakeAdb
    from fake_mtp import FakeMtp
    from wristpull.agent import queue
    from wristpull.agent.deps import AgentDeps
    from wristpull.agent.runner import AgentRunner
    from wristpull.db import connect

    now = [datetime(2026, 10, 7, 6, 0, tzinfo=UTC)]
    client = client_for(seeded, password_hash, now=lambda: now[0])
    login(client)
    page = client.get("/sync", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Geräte-Agent" in page.text and "nicht erreichbar" in page.text and "Noch keine Geräte-Jobs" in page.text
    assert 'name="kind" value="pull_strap"' in page.text and "disabled" in page.text  # no band configured
    today = client.get("/")
    assert "Devices" in today.text and 'href="/sync"' in today.text and "offline" in today.text
    token = csrf_of(page.text)
    assert client.post("/sync/run", data={"kind": "import_garmin"}).status_code == 403  # no CSRF token
    assert client.post("/sync/run", data={"csrf": token, "kind": "mount"}).status_code == 400
    assert client.post("/sync/run", data={"csrf": token, "kind": "pull_phone", "wait": "abc"}).status_code == 400
    queued = client.post("/sync/run", data={"csrf": token, "kind": "import_garmin", "dry_run": "1"}, headers={"hx-request": "true"})
    assert queued.status_code == 200 and "queued" in queued.text and 'hx-get="/sync/job?poll=1"' in queued.text
    assert "agent is offline" in queued.text and "Stop after the current file" in queued.text
    with connect(seeded.db_path) as conn:
        job = queue.current(conn)
        assert job.kind == "import_garmin" and job.params == {"dry_run": True, "auto": False} and job.cli == "wristpull import garmin --dry-run"
    busy = client.post("/sync/run", data={"csrf": token, "kind": "pull_strap"}, headers={"hx-request": "true"})
    assert "Another job is still running" in busy.text
    plain = client.post("/sync/run", data={"csrf": token, "kind": "pull_strap"})
    assert plain.status_code == 303 and plain.headers["location"] == "/sync"
    # the agent runs it (one cycle with the fake watch) and the next poll reloads the page once
    deps = AgentDeps(adb_factory=lambda: FakeAdb(tmp_path / "phone", devices=[]), mtp_factory=lambda: FakeMtp(garmin_tree.parent), sleep=lambda _s: None,
                     today=lambda: TODAY, now=lambda: now[0], sysfs=tmp_path / "no-sysfs")
    runner = AgentRunner(None, deps, load=lambda _p: seeded)
    done = runner.run_once()
    assert done.status == "done"
    poll = client.get("/sync/job?poll=1", headers={"accept-language": "de"})
    assert poll.headers.get("hx-refresh") == "true" and "hx-get" not in poll.text
    assert "Garmin importieren fertig nach" in poll.text and "10 FIT-Dateien, 0 neu, 10 bereits archiviert" in poll.text  # the seeded files are archived already
    assert "▶ Import Garmin" in poll.text and "$ wristpull import garmin --dry-run" in poll.text and 'data-copy="wristpull import garmin --dry-run"' in poll.text
    page = client.get("/sync")
    assert "online" in page.text and "Recent jobs" in page.text and 'class="chip state-done"' in page.text and "not attached" in page.text
    today = client.get("/", headers={"accept-language": "de"})
    assert "Letzter Geräte-Job: Garmin importieren, fertig" in today.text or "Letzter Geräte-Job" in today.text
    # stop: flags the active job; the partial then hides the stop button
    with connect(seeded.db_path) as conn:
        job = queue.enqueue(conn, "import_garmin", {}, now=now[0])
    stopped = client.post("/sync/stop", data={"csrf": token, "job_id": str(job.id)}, headers={"hx-request": "true"})
    assert stopped.status_code == 200 and "Stop after the current file" not in stopped.text
    with connect(seeded.db_path) as conn:
        assert queue.get(conn, job.id).stop_requested
    assert client.post("/sync/stop", data={"csrf": token, "job_id": "x"}).status_code == 400
    assert runner.run_once().status == "stopped"
    assert "stopped after" in client.get("/sync/job").text
    # the agent's heartbeat ages out: offline again
    now[0] = now[0].replace(hour=8)
    assert "offline" in client.get("/").text
    assert client.get("/sync/job").status_code == 200
    anonymous = TestClient(client.app, base_url="https://testserver", follow_redirects=False)
    assert anonymous.get("/sync").status_code == 303 and anonymous.get("/sync/job").status_code == 303


# ---- settings, the secrets store, the start password (parity W3) ---------------------------------


def test_first_login_with_the_start_password_forces_a_change(seeded: Config, password_hash: str):
    from wristpull import secrets as store

    client = client_for(seeded, password_hash=None, env={})
    page = client.get("/login")
    assert "start password" in page.text
    token = csrf_of(page.text)
    assert client.post("/login", data={"csrf": token, "password": "nope"}).status_code == 401
    first = client.post("/login", data={"csrf": token, "password": "wristpull"})
    assert first.status_code == 303 and first.headers["location"] == "/settings/password?first=1"
    assert client.get("/").headers["location"] == "/settings/password?first=1"  # nothing else until the password is changed
    assert client.get("/sync").status_code == 303
    page = client.get("/settings/password?first=1")
    assert page.status_code == 200 and "logged in with the start password" in page.text
    token = csrf_of(page.text)
    assert client.post("/sync/run", data={"csrf": token, "kind": "import_garmin"}).status_code == 303  # POSTs are redirected too
    weak = client.post("/settings/password", data={"csrf": token, "current": "wristpull", "new": "short", "repeat": "short"})
    assert weak.status_code == 400 and "Too short" in weak.text
    wrong = client.post("/settings/password", data={"csrf": token, "current": "wristpull", "new": "longer password", "repeat": "other"})
    assert wrong.status_code == 400 and "differ" in wrong.text
    bad_current = client.post("/settings/password", data={"csrf": token, "current": "x", "new": "longer password", "repeat": "longer password"})
    assert bad_current.status_code == 400 and "current password is wrong" in bad_current.text
    done = client.post("/settings/password", data={"csrf": token, "current": "wristpull", "new": "longer password", "repeat": "longer password"})
    assert done.status_code == 303 and done.headers["location"] == "/?saved=password"
    stored = store.load(seeded)[store.PASSWORD_HASH]
    assert stored.startswith("scrypt:") and verify_password("longer password", stored)
    assert client.get("/").status_code == 200  # the forced change is over
    # the start password no longer works, the new one does, and a hash in the environment is overridden by the store
    fresh = client_for(seeded, password_hash=password_hash, env={})
    page = fresh.get("/login")
    assert "start password" not in page.text
    assert fresh.post("/login", data={"csrf": csrf_of(page.text), "password": "wristpull"}).status_code == 401
    assert fresh.post("/login", data={"csrf": csrf_of(page.text), "password": PASSWORD}).status_code == 401  # the env hash lost
    assert fresh.post("/login", data={"csrf": csrf_of(page.text), "password": "longer password"}).status_code == 303
    # a logged-in user changes the password again from the settings page
    page = fresh.get("/settings")
    assert page.status_code == 200 and "Set on this page" in page.text
    token = csrf_of(page.text)
    again = fresh.post("/settings/password", data={"csrf": token, "current": "longer password", "new": "even longer password", "repeat": "even longer password"})
    assert again.status_code == 303 and again.headers["location"] == "/settings?saved=password"
    assert verify_password("even longer password", store.load(seeded)[store.PASSWORD_HASH])


def test_settings_page_saves_config_and_keys(seeded: Config, password_hash: str, tmp_path: Path):
    from wristpull import secrets as store
    from wristpull.config import load_config

    seeded.path = tmp_path / "cfg" / "config.toml"
    env = {"UID": "1000", "WRISTPULL_DATA": "/home/alice/Health/wristpull", "ANTHROPIC_API_KEY": "env-key"}
    client = client_for(seeded, password_hash, env=env)
    login(client)
    page = client.get("/settings", headers={"accept-language": "de"})
    assert page.status_code == 200 and "Einstellungen" in page.text and "/home/alice/Health/wristpull" in page.text
    assert "env-key" not in page.text and "aus der .env" in page.text  # the key's source is shown, never its value
    token = csrf_of(page.text)
    bad = client.post("/settings/config", data={"csrf": token, "timezone": "Europe/Berlin", "strap_address": "nope", "strap_since_days": "x", "coach_model": "m"})
    assert bad.status_code == 400 and "strap address must look like" in bad.text and "whole number" in bad.text and not seeded.path.exists()
    form = {"csrf": token, "timezone": "Europe/Vienna", "folders": "Activity, Monitor", "phone_serial": "ABC1", "phone_export_dir": "/sdcard/GB",
            "strap_address": "aa:bb:cc:dd:ee:02", "strap_adapter": "", "strap_since_days": "7", "strap_keep_on_band": "1",
            "ui_language": "de", "ui_range_days": "14", "coach_backend": "claude", "coach_model": "claude-opus-5-5", "coach_weekly_model": "",
            "coach_api_key_env": "", "coach_ollama_url": "http://localhost:11434", "coach_num_ctx": "4096", "coach_max_tokens": "2000",
            "coach_timeout_s": "120", "coach_token_budget": "5000", "coach_effort": "high", "coach_language": "de"}
    saved = client.post("/settings/config", data=form)  # agent_import_on_plug unchecked -> false
    assert saved.status_code == 303 and saved.headers["location"] == "/settings?saved=config"
    written = load_config(seeded.path)
    assert written.timezone == "Europe/Vienna" and written.garmin_folders == ["Activity", "Monitor"] and written.phone_serial == "ABC1"
    assert written.strap_address == "AA:BB:CC:DD:EE:02" and written.strap_since_days == 7 and written.strap_keep_on_band and not written.agent_import_on_plug
    assert written.ui_language == "de" and written.ui_range_days == 14 and written.coach.backend == "claude" and written.coach.effort == "high" and written.coach.timeout_s == 120
    assert seeded.strap_address == "AA:BB:CC:DD:EE:02" and seeded.coach.backend == "claude"  # the running app follows
    page = client.get("/settings?saved=config")  # the saved [ui] language = de applies at once
    assert "Konfiguration nach" in page.text and 'value="Europe/Vienna"' in page.text and "Adresse des Bands unter" not in client.get("/sync").text
    # keys: write-only, the store wins over .env, "remove" clears
    keys = client.post("/settings/secrets", data={"csrf": token, "WRISTPULL_STRAP_AUTHKEY": "0x" + "ab" * 16, "ANTHROPIC_API_KEY": "", "ollama_url": "http://host.docker.internal:11434"})
    assert keys.status_code == 303
    assert store.load(seeded) == {"WRISTPULL_STRAP_AUTHKEY": "0x" + "ab" * 16, "WRISTPULL_OLLAMA_URL": "http://host.docker.internal:11434"}
    assert seeded.coach.ollama_url == "http://host.docker.internal:11434"
    page = client.get("/settings")
    assert "0xab" not in page.text and "aus den Einstellungen" in page.text and 'value="http://host.docker.internal:11434"' in page.text
    assert "Schlüssel aus der Umgebung" in client.get("/sync").text  # has_auth_key sees the applied store
    cleared = client.post("/settings/secrets", data={"csrf": token, "clear_WRISTPULL_STRAP_AUTHKEY": "1", "ollama_url": ""})
    assert cleared.status_code == 303 and store.load(seeded) == {}
    assert client.post("/settings/config", data={"timezone": "x"}).status_code == 403  # CSRF
