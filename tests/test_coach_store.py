import pytest

from wristpull.coach.store import add_feedback, add_note, get_run, last_weekly_plan, memory_digest, notes, recent_notes, recent_runs, save_run


def _run(conn, kind="daily", answer="## Status\nok\n## Today's suggestion\nEasy 40 min run.\n## Numbers used\n52", **extra):
    base = dict(kind=kind, period_start="2026-10-01", period_end="2026-10-06", language="en", backend="fake", model="m", prompt_sha256="x", system_prompt="s", user_prompt="u", answer_md=answer)
    base.update(extra)
    return save_run(conn, **base)


def test_save_get_recent_and_feedback(conn):
    first = _run(conn, input_tokens=100, output_tokens=20, cost_usd=0.001)
    second = _run(conn, kind="ask", question="why?", status="ok")
    dry = _run(conn, status="dry_run", answer=None)
    assert get_run(conn, first)["kind"] == "daily" and get_run(conn, first)["tokens_estimated"] == 0
    assert get_run(conn, 999) is None
    assert [r["id"] for r in recent_runs(conn)] == [second, first]
    assert [r["id"] for r in recent_runs(conn, status="dry_run")] == [dry]
    assert recent_runs(conn, kind="ask")[0]["question"] == "why?"
    add_feedback(conn, first, 1, "good")
    assert recent_runs(conn, kind="daily")[0]["rating"] == 1
    with pytest.raises(ValueError):
        add_feedback(conn, first, 5)
    with pytest.raises(ValueError):
        add_feedback(conn, 999, 1)
    with pytest.raises(ValueError):
        save_run(conn, bogus=1)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"


def test_memory_digest_picks_suggestions_and_plans(conn):
    assert memory_digest(conn, "en") == ""
    a = _run(conn)
    add_feedback(conn, a, -1)
    _run(conn, kind="weekly", answer="## Week in review\nfine\n## Plan for next week\n| Mon | rest |\n## Numbers used\n1")
    _run(conn, answer="no headings at all " * 40)
    digest = memory_digest(conn, "en")
    lines = digest.splitlines()
    assert len(lines) == 3 and lines[0].startswith("- 20") and " UTC daily" in lines[0]
    assert any("| Mon | rest |" in line for line in lines) and any("👎" in line for line in lines)
    assert all(len(line) < 460 for line in lines)
    assert memory_digest(conn, "en", max_chars=50) == ""


def test_journal_notes(conn):
    add_note(conn, "2026-10-05", "legs heavy")
    add_note(conn, "2026-10-05", "slept badly")
    add_note(conn, "2026-10-07", "great run")
    assert notes(conn, "2026-10-01", "2026-10-06") == [{"local_date": "2026-10-05", "note": "legs heavy · slept badly"}]
    assert [n["local_date"] for n in recent_notes(conn, 5)] == ["2026-10-07", "2026-10-05"]
    with pytest.raises(ValueError):
        add_note(conn, "2026-10-08", "   ")


def test_last_weekly_plan(conn):
    assert last_weekly_plan(conn, "2026-10-12") is None
    _run(conn, kind="weekly", period_start="2026-09-28", period_end="2026-10-04", answer="## Week in review\nx\n## Plan for next week\n| Mon | Easy run | 40 min | easy |\n## Numbers used\n1")
    _run(conn, kind="weekly", period_start="2026-10-05", period_end="2026-10-11", answer="## Week in review\nno plan section\n")
    run, plan = last_weekly_plan(conn, "2026-10-12")
    assert run["period_end"] == "2026-10-04" and "| Mon | Easy run |" in plan  # the run without a plan is skipped
    assert last_weekly_plan(conn, "2026-10-01") is None
