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
    path.write_text(f'[storage]\ndata_dir = "{tmp_path / "Health"}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
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


def client_for(config: Config, password_hash: str, limiter: RateLimiter | None = None) -> TestClient:
    app = create_app(config, password_hash=password_hash, secret=SECRET, deps=WebDeps(today=lambda: TODAY, limiter=limiter or RateLimiter()))
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
    trends = pages.trends_data(conn, TODAY, 4)
    assert trends["weeks"] == 4 and trends["start"] == "2026-09-10" and len(trends["charts"][0]["labels"]) == 28
    rhr = trends["charts"][0]["series"][1]["data"]
    assert all(v is None for v in rhr)  # no strap data: gaps, not zeros
    assert trends["charts"][-1]["kind"] == "acwr" and all(v is None for v in trends["charts"][-1]["series"][0]["data"])  # < 28 days history
    assert pages.trends_data(conn, TODAY, 7)["weeks"] == 4  # unknown choice falls back
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
    assert "Uhr (USB)" in home.text and home.headers["cache-control"] == "no-store"
    trends = client.get("/trends?weeks=12")
    assert trends.status_code == 200 and 'id="chart-rhr"' in trends.text and '"labels":[' in trends.text
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
    nights = pages.sleep_list(conn, TODAY, 4)
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
    assert night.status_code == 200 and "Schlafphasen" in night.text and 'class="seg ' in night.text
    assert client.get("/sleep/99999").status_code == 404 and client.get("/sleep/abc").status_code == 404


# ---- serve ------------------------------------------------------------------------------


def test_serve_requires_hash_runs_one_worker_and_warns_without_secret(config: Config, password_hash: str, tmp_path: Path):
    calls, warnings = [], []
    assert serve(config, env={}, run=lambda *a, **k: calls.append(k), warn=warnings.append) == 1
    assert calls == [] and PASSWORD_ENV in warnings[0]
    env = {PASSWORD_ENV: password_hash}
    assert serve(config, env=env, data_dir=tmp_path / "d", behind_proxy=True, run=lambda app, **k: calls.append((app, k)), warn=warnings.append) == 0
    app, kwargs = calls[0]
    assert kwargs["workers"] == 1 and kwargs["proxy_headers"] is True and kwargs["forwarded_allow_ips"] == "*"
    assert SECRET_ENV in warnings[-1]
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
    assert main(["--config", str(cfg_file), "serve"]) == 1
    assert "WRISTPULL_WEB_PASSWORD_HASH is missing" in capsys.readouterr().err


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
    ids.update({"Resting HR", "HRV last night", "Sleep", "Stress", "Training per week", "Acute:chronic load", "Watch", "Strap", "Hours", "ACWR"})
    missing = sorted(i for i in ids if de.gettext(i) == i and i not in {"Trends", "Stress", "Strap", "Sport", "Body Battery", "bpm", "ms", "h", "max", "ACWR", "Score {score}", "m", "rem"})
    assert not missing, missing
    assert de.ngettext("{n} warning sign", "{n} warning signs", 2) == "{n} Warnzeichen"
