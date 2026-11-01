from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Checkbox, DataTable, Input, RadioSet  # noqa: E402

from tui_helpers import make_app, make_deps, run, wait_jobs  # noqa: E402
from wristpull.coach.backends import FakeBackend  # noqa: E402
from wristpull.coach.profile import load_answers, profile_path  # noqa: E402
from wristpull.coach.store import recent_runs  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.importers.garmin import import_garmin  # noqa: E402
from wristpull.tui.screens.coach import CoachScreen  # noqa: E402
from wristpull.tui.screens.coach_wizard import WizardScreen  # noqa: E402
from wristpull.tui.widgets import Panel  # noqa: E402

ANSWER = "## Status\nFresh.\n\n## Today's suggestion\nEasy 30 min run.\n\n## Watch out for\n- nothing\n\n## Numbers used\n47\n"
ASK = "## Answer\nYes, go.\n\n## Numbers used\n47\n"


def filled_profile(config: Config) -> None:
    path = profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Goal profile\n- Goals: run\n", "utf-8")


def test_coach_screen_runs_rates_and_asks(config: Config, tmp_path: Path, garmin_tree: Path):
    deps = make_deps(tmp_path, garmin_tree)
    fake = FakeBackend([ANSWER, ASK])
    deps.coach_backend = lambda cfg, **kw: fake
    app = make_app(config, deps)
    import_garmin(app.conn, config, path=garmin_tree)
    filled_profile(config)

    async def scenario():
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.press("9")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CoachScreen)
            status = str(screen.query_one("#coach-status", Panel).content)
            assert "ollama" in status and "profile.md  ✓" in status
            assert "No coach runs yet" in str(screen.query_one("#coach-answer", Panel).content)
            await pilot.press("d")
            await wait_jobs(app)
            await pilot.pause()
            table = screen.query_one("#runs", DataTable)
            assert table.row_count == 1 and "Easy 30 min run" in str(screen.query_one("#coach-answer", Panel).content)
            assert fake.requests[0].model == "fake-model" and app.runner.last.spec.cli == "wristpull coach daily"
            await pilot.press("plus")
            await pilot.pause()
            assert recent_runs(app.conn)[0]["rating"] == 1
            # a question through the input field
            box = screen.query_one("#question", Input)
            box.focus()
            box.value = "Long run today?"
            await pilot.press("enter")
            await wait_jobs(app)
            await pilot.pause()
            assert table.row_count == 2 and "Yes, go." in str(screen.query_one("#coach-answer", Panel).content)
            assert 'wristpull coach ask "Long run today?"' == app.runner.last.spec.cli
            # dry run leaves a run row without sending
            await pilot.press("p")
            await wait_jobs(app)
            await pilot.pause()
            assert len(fake.requests) == 2 and "Dry run" in str(screen.query_one("#coach-answer", Panel).content)
            # journal note through the same input field (n toggles the mode)
            await pilot.press("n")
            await pilot.pause()
            assert screen.input_mode == "note"
            box.value = "legs heavy"
            await pilot.press("enter")
            await pilot.pause()
            from wristpull.coach.store import recent_notes

            assert recent_notes(app.conn, 1)[0]["note"] == "legs heavy" and screen.input_mode == "ask"
            assert "Journal 2026-10-06: legs heavy" in str(screen.query_one("#coach-status", Panel).content)
            # editing goes through the injected hook
            opened: list[Path] = []
            deps.editor = opened.append
            await pilot.press("e")
            await pilot.pause()
            assert opened == [profile_path(config)]

    run(scenario())


def test_coach_screen_shows_open_placeholders_and_wizard_writes_profile(config: Config, tmp_path: Path):
    deps = make_deps(tmp_path)
    deps.coach_backend = lambda cfg, **kw: FakeBackend([ANSWER])
    app = make_app(config, deps)

    async def scenario():
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.press("9")
            await pilot.pause()
            screen = app.screen
            assert "open placeholders" in str(screen.query_one("#coach-status", Panel).content)
            await pilot.press("g")
            await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, WizardScreen)
            async def pick(selector: str) -> None:
                wizard.query_one(selector).scroll_visible(animate=False)
                await pilot.pause()
                await pilot.click(selector)
                await pilot.pause()

            wizard.query_one("#q-goals-endurance", Checkbox).value = True
            wizard.query_one("#q-event", Input).value = "5000 m in 22 min"
            await pick("#q-weekly_hours-3to5")
            wizard.query_one("#q-sessions", Input).value = "4 sessions, rest on Saturday"
            await pick("#q-experience-intermediate")
            wizard.query_one("#q-preferences-run_outdoor", Checkbox).value = True
            wizard.query_one("#q-constraints", Input).value = "none"
            await pick("#q-style-balanced")
            await pick("#q-tone-terse")
            wizard.query_one("#q-details", Input).value = "51 years"
            await pilot.pause()
            assert wizard.query_one("#q-style", RadioSet).pressed_button is not None
            wizard.action_save()
            await pilot.pause()
            assert isinstance(app.screen, CoachScreen)
            assert "profile.md  ✓" in str(app.screen.query_one("#coach-status", Panel).content)
            answers = load_answers(config)
            assert answers["goals"] == ["endurance"] and answers["weekly_hours"] == "3to5" and answers["style"] == "balanced"
            text = profile_path(config).read_text("utf-8")
            assert "improve endurance and running performance" in text and "## My notes" in text
            # cancel leaves everything as it is
            await pilot.press("g")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, CoachScreen) and load_answers(config) == answers

    run(scenario())
