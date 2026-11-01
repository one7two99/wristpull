from datetime import date
from pathlib import Path

import pytest

from wristpull.cli import main
from wristpull.coach.backends import CoachError, FakeBackend
from wristpull.coach.profile import profile_path
from wristpull.coach.prompts import parse_sections, system_prompt, unfilled_placeholders, user_prompt, verify_numbers
from wristpull.coach.runner import cost_estimate, resolve_language, run_coach
from wristpull.coach.store import recent_runs
from wristpull.config import Config
from wristpull.importers.garmin import import_garmin

TODAY = date(2026, 10, 6)
ANSWER_EN = "## Status\nRecovered.\n\n## Today's suggestion\nEasy 40 min run in zone 2.\n\n## Watch out for\n- nothing\n\n## Numbers used\nRHR 47, HRV 40\n"
ANSWER_DE = "## Status\nErholt.\n## Vorschlag für heute\n40 min locker.\n## Worauf achten\n- nichts\n## Verwendete Zahlen\nRHR 47\n"


def filled_profile(config: Config, lang: str = "en") -> Path:
    path = profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Goal profile\n- Goals: run a half marathon in spring\n- Constraints: none\n", "utf-8")
    return path


def test_prompts_contract_and_parser():
    assert "## Today's suggestion" in system_prompt("daily", "en") and "Answer in German" in system_prompt("daily", "de")
    user = user_prompt("ask", profile_md="P", data_md="D", digest_md="", question="How hard today?", lang="en")
    assert user.index("# Goal profile") < user.index("# Data") < user.index("# Task") < user.index("# Output format")
    assert "Question: How hard today?" in user and "(none yet)" in user
    sections = parse_sections(ANSWER_EN, "daily", "en")
    assert sections["suggestion"] == "Easy 40 min run in zone 2." and sections["numbers"] == "RHR 47, HRV 40"
    assert parse_sections(ANSWER_DE, "daily", "en")["suggestion"] == "40 min locker."  # German headings are recognised too
    assert parse_sections("### **Status:**\nok\n## Vorschlag für heute\nx", "daily", "de") == {"status": "ok", "suggestion": "x"}
    assert parse_sections("plain text answer", "daily", "en") == {"status": "plain text answer"}
    assert unfilled_placeholders("a {{goals}} b {{goals}} {{age}} {{not a placeholder}}") == ["age", "goals"]


def test_run_coach_daily_and_ask_with_fake_backend(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    filled_profile(config)
    fake = FakeBackend([ANSWER_EN])
    logs: list[str] = []
    result = run_coach(conn, config, "daily", today=TODAY, backend=fake, log=logs.append)
    assert result.status == "ok" and result.sections["suggestion"].startswith("Easy 40") and result.language == "en"
    assert result.input_tokens and result.tokens_estimated and result.cost_usd is None  # fake model has no price
    assert "# Goal profile" in fake.requests[0].user and "half marathon" in fake.requests[0].user
    assert "## Last 7 days" in fake.requests[0].user and "## Today's suggestion" in fake.requests[0].system
    assert any("sending" in line for line in logs) and any("answer in" in line for line in logs)
    stored = recent_runs(conn)[0]
    assert stored["kind"] == "daily" and stored["answer_md"] == ANSWER_EN and stored["profile_sha256"]
    # the next run sees the first one in its digest
    ask = run_coach(conn, config, "ask", today=TODAY, question="Should I rest?", backend=FakeBackend(["## Answer\nYes.\n## Numbers used\n-"]), language="de")
    assert ask.language == "de" and ask.sections["answer"] == "Yes."
    assert "Easy 40 min run" in recent_runs(conn, kind="ask")[0]["user_prompt"]
    assert "Question: Should I rest?" in recent_runs(conn, kind="ask")[0]["user_prompt"]
    with pytest.raises(CoachError, match="question is required"):
        run_coach(conn, config, "ask", today=TODAY, backend=fake)
    with pytest.raises(CoachError, match="unknown coach run kind"):
        run_coach(conn, config, "monthly", today=TODAY, backend=fake)


def test_run_coach_profile_placeholders_dry_run_and_errors(conn, config):
    fake = FakeBackend([ANSWER_EN])
    with pytest.raises(CoachError, match="unfilled placeholders"):
        run_coach(conn, config, "daily", today=TODAY, backend=fake)
    assert profile_path(config).exists()  # the default profile was created for editing
    result = run_coach(conn, config, "daily", today=TODAY, backend=fake, dry_run=True)
    assert result.status == "dry_run" and result.dry_run_path and result.dry_run_path.exists()
    assert "# System prompt" in result.dry_run_path.read_text("utf-8") and fake.requests == []
    assert any("placeholders" in w for w in result.warnings)
    assert recent_runs(conn, status="dry_run")[0]["kind"] == "daily"
    forced = run_coach(conn, config, "daily", today=TODAY, backend=fake, force=True)
    assert forced.status == "ok" and any("placeholders" in w for w in forced.warnings)
    failing = FakeBackend(fail=CoachError("Ollama not reachable"))
    with pytest.raises(CoachError, match="not reachable"):
        run_coach(conn, config, "daily", today=TODAY, backend=failing, force=True)
    assert recent_runs(conn, status="error")[0]["error"] == "Ollama not reachable"


def test_truncated_answer_and_cost_estimate(conn, config):
    filled_profile(config)
    config.coach.prices["fake-model"] = (1.0, 10.0)
    fake = FakeBackend([ANSWER_EN])
    original = fake.complete

    def complete(request, *, log=lambda _m: None):
        response = original(request, log=log)
        response.stop_reason = "length"
        response.input_tokens, response.output_tokens = 1_000_000, 100_000
        return response

    fake.complete = complete  # type: ignore[method-assign]
    result = run_coach(conn, config, "daily", today=TODAY, backend=fake)
    assert any("cut off" in w for w in result.warnings) and result.cost_usd == 2.0
    assert cost_estimate(config, "unknown-model", 1, 1) is None
    assert resolve_language(Config(ui_language="de"), None) == "de" and resolve_language(Config(), "de") == "de"
    config.coach.language = "en"
    assert resolve_language(config, None) == "en"


def test_coach_cli(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndata_dir = "{tmp_path / "Health"}"\n[time]\ntimezone = "Europe/Berlin"\n[coach]\nbackend = "ollama"\nmodel = "gemma3:4b"\n', "utf-8")
    assert main(["--config", str(cfg), "coach", "show"]) == 1
    assert "no coach runs" in capsys.readouterr().out
    assert main(["--config", str(cfg), "coach", "profile"]) == 2  # default profile created, placeholders open
    out = capsys.readouterr().out
    assert "profile:" in out and "goals" in out
    from wristpull import cli

    monkeypatch.setattr(cli, "make_backend", lambda config, model=None, backend=None, kind="daily": FakeBackend([ANSWER_EN]), raising=False)
    monkeypatch.setattr("wristpull.coach.backends.make_backend", lambda config, model=None, backend=None, kind="daily": FakeBackend([ANSWER_EN]))
    assert main(["--config", str(cfg), "coach", "daily", "--date", "2026-10-06", "--dry-run"]) == 0
    assert "dry run #1" in capsys.readouterr().out
    assert main(["--config", str(cfg), "coach", "daily", "--date", "2026-10-06"]) == 1  # placeholders, no --force
    assert "unfilled placeholders" in capsys.readouterr().err
    assert main(["--config", str(cfg), "-q", "coach", "daily", "--date", "2026-10-06", "--force"]) == 0
    out = capsys.readouterr()
    assert "Easy 40 min run" in out.out and "[run #2, fake/fake-model" in out.err
    assert main(["--config", str(cfg), "coach", "show", "--last", "2"]) == 0
    assert "--- #2 daily" in capsys.readouterr().out
    assert main(["--config", str(cfg), "coach", "rate", "2", "1", "--note", "did the walk"]) == 0
    assert "rated run #2: +1" in capsys.readouterr().out
    assert main(["--config", str(cfg), "coach", "rate", "99", "1"]) == 2
    capsys.readouterr()
    assert main(["--config", str(cfg), "coach", "show"]) == 0
    assert "👍" in capsys.readouterr().out
    monkeypatch.setattr("wristpull.coach.backends.make_backend", lambda config, model=None, backend=None, kind="daily": FakeBackend([WEEKLY_EN]))
    assert main(["--config", str(cfg), "-q", "coach", "weekly", "--date", "2026-10-11", "--force"]) == 0
    assert "Plan for next week" in capsys.readouterr().out


WEEKLY_EN = "## Week in review\nLight week.\n## Recovery\nfine\n## Training load\nlow\n## Plan for next week\n| Mon | rest |\n## Questions for you\n- ?\n## Numbers used\n47, 78, 30\n"


def test_run_coach_weekly_and_number_check(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    filled_profile(config)
    fake = FakeBackend([WEEKLY_EN])
    result = run_coach(conn, config, "weekly", today=date(2026, 10, 11), backend=fake)
    assert result.status == "ok" and result.sections["plan"] == "| Mon | rest |"
    assert "## Week in review" in fake.requests[0].system and "## Week vs previous week" in fake.requests[0].user
    assert recent_runs(conn, kind="weekly")[0]["period_start"] == "2026-10-05"
    assert not any("numbers not found" in w for w in result.warnings)  # 47, 78, 30 occur in the data
    liar = FakeBackend(["## Status\nx\n## Today's suggestion\ny\n## Watch out for\nz\n## Numbers used\n47, 999, 12.5, 3\n"])
    result = run_coach(conn, config, "daily", today=TODAY, backend=liar)
    assert any(w.startswith("numbers not found in the data package: 999, 12.5") for w in result.warnings)


def test_verify_numbers():
    data = "resting HR 52 bpm, slept 7.4 h, steps 4766, load 61.5, score 86"
    assert verify_numbers("RHR 52, sleep 7,4 h, 4766 steps, 61.50 load, 3 zones", data) == []
    assert verify_numbers("RHR 53 and 7.9 h", data) == ["53", "7.9"]
    assert verify_numbers("ignored 999", data, section="52, 86") == []
    assert verify_numbers("7 h of sleep", data) == []  # integer part of a decimal in the data is accepted
    profile = "Goal: 5000m in 22min within 3 months, max HR 180bpm, age 51"
    assert verify_numbers("aim for 5000 m in 22 minutes at max 180", profile) == []  # units glued to numbers in the profile
