from pathlib import Path

from wristpull.cli import main
from wristpull.coach.profile import answers_path, apply_wizard, cli_asker, load_answers, load_profile, profile_path, run_wizard
from wristpull.coach.prompts import DEFAULT_PROFILE, unfilled_placeholders
from wristpull.coach.wizard import MARK_END, MARK_START, QUESTIONS, answers_to_toml, generate_goal_prompt, merge_profile
from wristpull.config import Config

ANSWERS = {
    "goals": ["endurance", "event"],
    "event": "5000 m in 22 min on 2027-02-01",
    "weekly_hours": "3to5",
    "sessions": "4 sessions, rest on Saturday",
    "experience": "intermediate",
    "preferences": ["run_outdoor", "strength_home"],
    "constraints": "none",
    "style": "balanced",
    "tone": "terse",
    "details": "51 years, 70 kg, max HR 180",
}


def test_generate_goal_prompt_both_languages_and_placeholders():
    en = generate_goal_prompt(ANSWERS, "en")
    assert en.startswith(MARK_START) and en.endswith(MARK_END)
    assert "- Goals: improve endurance and running performance; prepare for a concrete event" in en
    assert "- Event: 5000 m in 22 min on 2027-02-01" in en and "3 to 5 hours per week; 4 sessions, rest on Saturday" in en
    assert "running outdoors, strength at home" in en and "Plan balanced" in en and "Answer tersely" in en
    assert unfilled_placeholders(en) == []
    de = generate_goal_prompt(ANSWERS, "de")
    assert "# Zielprofil" in de and "Ausdauer und Laufleistung verbessern" in de and "Knapp antworten" in de
    partial = generate_goal_prompt({"goals": ["fitness"]}, "en")
    assert unfilled_placeholders(partial) == ["constraints", "experience", "preferences", "sessions_and_rest_day", "style", "weekly_hours"]
    assert "- Event: none" in partial


def test_merge_profile_keeps_notes_and_replaces_block():
    first = merge_profile(None, generate_goal_prompt(ANSWERS, "en"), "en")
    assert first.count(MARK_START) == 1 and "## My notes" in first
    edited = first.replace("(anything else", "I run with a friend on Tuesdays. (anything else")
    second = merge_profile(edited, generate_goal_prompt({**ANSWERS, "style": "cautious"}, "en"), "en")
    assert "Plan cautiously" in second and "Plan balanced" not in second
    assert "I run with a friend on Tuesdays." in second and second.count(MARK_START) == 1
    legacy = "# My own profile\n- Goal: run more\n"
    merged = merge_profile(legacy, generate_goal_prompt(ANSWERS, "en"), "en")
    assert merged.index(MARK_END) < merged.index("# My own profile")
    toml = answers_to_toml(ANSWERS)
    assert 'goals = ["endurance", "event"]' in toml and 'tone = "terse"' in toml


def test_apply_wizard_backs_up_and_stores_answers(config: Config):
    path = apply_wizard(config, ANSWERS, "en")
    assert path == profile_path(config) and load_answers(config) == ANSWERS
    text, _sha, placeholders = load_profile(config, "en")
    assert placeholders == [] and MARK_START in text
    # the untouched default template is replaced without a backup; a customised file is backed up
    assert not list(path.parent.glob("profile.2*.md"))
    path.write_text(text + "\nMy extra line\n", "utf-8")
    apply_wizard(config, {**ANSWERS, "tone": "detailed"}, "en")
    backups = list(path.parent.glob("profile.2*.md"))
    assert len(backups) == 1 and "My extra line" in backups[0].read_text("utf-8")
    assert "My extra line" in path.read_text("utf-8") and "Answer in detail" in path.read_text("utf-8")
    # default template counts as untouched
    path.write_text(DEFAULT_PROFILE["en"], "utf-8")
    apply_wizard(config, ANSWERS, "en")
    assert len(list(path.parent.glob("profile.2*.md"))) == 1
    answers_path(config).write_text("not = valid = toml", "utf-8")
    assert load_answers(config) == {}


def test_cli_asker_and_run_wizard(config: Config):
    scripted = iter(["1,4", "5000 m in 22 min", "2", "4 sessions, rest Saturday", "", "1,4", "none", "2", "1", ""])
    printed: list[str] = []
    ask = cli_asker("en", input_fn=lambda prompt: next(scripted), print_fn=printed.append)
    answers = run_wizard(config, "en", ask)
    assert answers["goals"] == ["endurance", "event"] and answers["weekly_hours"] == "3to5"
    assert "experience" not in answers and answers["preferences"] == ["run_outdoor", "strength_home"] and answers["style"] == "balanced"
    assert any("What are you training for?" in line for line in printed)
    # Enter keeps the previous answer, garbage keeps it too
    apply_wizard(config, answers, "en")
    scripted = iter(["", "", "", "", "x", "", "", "", "", ""])
    again = run_wizard(config, "en", cli_asker("en", input_fn=lambda prompt: next(scripted), print_fn=printed.append))
    assert again["goals"] == ["endurance", "event"] and again["event"] == "5000 m in 22 min"
    assert len(QUESTIONS) == 10


def test_profile_wizard_cli(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndata_dir = "{tmp_path / "Health"}"\n', "utf-8")
    scripted = iter(["2", "", "1", "3 sessions", "1", "3", "none", "1", "2", "60 years"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(scripted))
    assert main(["--config", str(cfg), "coach", "profile", "--wizard", "--lang", "de"]) == 0
    out = capsys.readouterr().out
    assert "profile written:" in out and "open placeholders: none" in out
    text = (tmp_path / "Health" / "coach" / "profile.md").read_text("utf-8")
    assert "# Zielprofil" in text and "Vorsichtig planen" in text and "## Meine Notizen" in text
