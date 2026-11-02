from datetime import date
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from tui_helpers import TODAY, make_app, make_deps, run, wait_jobs  # noqa: E402
from wristpull.coach.store import save_run  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.tui.screens.coach import CoachScreen  # noqa: E402
from wristpull.tui.screens.workouts import WorkoutsScreen  # noqa: E402
from wristpull.tui.widgets import Panel  # noqa: E402
from wristpull.workouts.model import Step, Workout  # noqa: E402
from wristpull.workouts.store import list_workouts, load, save, workouts_dir  # noqa: E402

PLAN = "## Week in review\nok\n## Plan for next week\n| Mon | Easy run | 40 min | easy |\n| Tue | Rest | | |\n## Numbers used\n1\n"


def test_workouts_screen_new_edit_push_verify(config: Config, tmp_path: Path, garmin_tree: Path):
    deps = make_deps(tmp_path, garmin_tree.parent)
    edited: list[Path] = []

    def editor(path: Path) -> None:  # the "user" changes the main part in the editor
        edited.append(path)
        path.write_text(path.read_text("utf-8").replace("run 40 min hr Z2", "run 30 min hr 135-150"), "utf-8")

    deps.editor = editor
    app = make_app(config, deps)
    save(config, Workout(name="Old one", day=TODAY - date.resolution * 3, steps=[Step("active", duration_s=600)]))

    async def scenario():
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("0")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, WorkoutsScreen)
            status = str(screen.query_one("#workouts-status", Panel).content)
            assert "Watch connected" in status and "1 drafts" in status and "Template for n: time" in status
            table = screen.query_one("#workout-table", DataTable)
            assert table.row_count == 1
            # n: template + editor hook, the edited file is shown
            await pilot.press("n")
            await pilot.pause()
            assert len(edited) == 1 and edited[0].name == f"{(TODAY + date.resolution).isoformat()}-easy-run-40-min.md"
            assert table.row_count == 2
            preview = str(screen.query_one("#workout-preview", Panel).content)
            assert "2 active 30:00  HR 135-150" in preview and "run 30 min hr 135-150" in preview
            # t toggles the template, n again makes a distance workout
            await pilot.press("t")
            await pilot.pause()
            assert "Template for n: distance" in str(screen.query_one("#workouts-status", Panel).content)
            await pilot.press("n")
            await pilot.pause()
            assert table.row_count == 3 and "5000 m" in str(screen.query_one("#workout-preview", Panel).content)
            # e opens the selected file again
            await pilot.press("e")
            await pilot.pause()
            assert len(edited) == 3
            # P pushes every upcoming valid workout to the fake watch
            await pilot.press("P")
            await wait_jobs(app)
            await pilot.pause()
            assert app.runner.last.ok and len(app.runner.last.result.written) == 2
            assert app.runner.last.spec.cli.startswith("wristpull workout push ")
            names = sorted(p.name for p in (garmin_tree / "NewFiles").glob("*.fit"))
            assert len(names) == 2
            assert "2 pushed" in str(screen.query_one("#workouts-status", Panel).content)
            # v: nothing under Workouts yet, then the watch "takes over" one file
            await pilot.press("v")
            await wait_jobs(app)
            await pilot.pause()
            assert app.runner.last.result.missing and not app.runner.last.result.verified
            (garmin_tree / "Workouts").mkdir()
            (garmin_tree / "Workouts" / "W.FIT").write_bytes((garmin_tree / "NewFiles" / names[0]).read_bytes())
            await pilot.press("v")
            await wait_jobs(app)
            await pilot.pause()
            assert len(app.runner.last.result.verified) == 1
            assert "1 verified" in str(screen.query_one("#workouts-status", Panel).content)
            # d: dry run of the selected workout goes to the out folder
            await pilot.press("d")
            await wait_jobs(app)
            await pilot.pause()
            assert app.runner.last.result.dry_run and (workouts_dir(config) / "out").is_dir()
            # Delete needs a second press
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("delete")
            await pilot.pause()
            assert "Press Delete again" in str(screen.query_one("#workouts-status", Panel).content) and table.row_count == 3
            await pilot.press("delete")
            await pilot.pause()
            assert table.row_count == 2 and len(list_workouts(config)) == 2
            # period shift
            await pilot.press("left")
            await pilot.pause()
            assert table.row_count == 0 and "No workouts in this period" in str(screen.query_one("#workout-preview", Panel).content)
            await pilot.press("right")
            await pilot.pause()
            assert table.row_count == 2

    run(scenario())


def test_drafts_from_the_coach_plan_via_both_screens(config: Config, tmp_path: Path, garmin_tree: Path):
    deps = make_deps(tmp_path, garmin_tree.parent)
    app = make_app(config, deps)
    save_run(app.conn, kind="weekly", period_start="2026-09-28", period_end="2026-10-04", language="en", backend="fake", model="m", prompt_sha256="x", system_prompt="s", user_prompt="u", answer_md=PLAN)
    (config.data_dir / "coach").mkdir(parents=True)
    (config.data_dir / "coach" / "profile.md").write_text("# Goal profile\n- Goals: run\n", "utf-8")

    async def scenario():
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("9")
            await pilot.pause()
            assert isinstance(app.screen, CoachScreen)
            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, WorkoutsScreen)
            items = list_workouts(config)
            assert [w.day for w in items] == [date(2026, 10, 5)] and items[0].name == "Easy run" and items[0].steps[1].duration_s == 25 * 60
            table = app.screen.query_one("#workout-table", DataTable)
            assert table.row_count == 1
            # c on the workouts screen: nothing new (the day has a file), no crash
            await pilot.press("c")
            await pilot.pause()
            assert len(list_workouts(config)) == 1
            assert load(Path(items[0].path)).status == "draft"

    run(scenario())


def test_watch_view_lists_removes_and_force_pushes(config: Config, tmp_path: Path, garmin_tree: Path):
    from wristpull.workouts.watch import backup_dir

    deps = make_deps(tmp_path, garmin_tree.parent)
    app = make_app(config, deps)
    save(config, Workout(name="Tomorrow", day=TODAY + date.resolution, steps=[Step("active", duration_s=600)]))

    async def scenario():
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("0")
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#workout-table", DataTable)
            # P pushes once, a second P has nothing left, f pushes again
            await pilot.press("P")
            await wait_jobs(app)
            await pilot.pause()
            assert len(app.runner.last.result.written) == 1
            await pilot.press("P")
            await pilot.pause()
            assert app.runner.last.spec.key == "workout_push" and len(list((garmin_tree / "NewFiles").glob("*.fit"))) == 1
            await pilot.press("f")
            await wait_jobs(app)
            await pilot.pause()
            assert "--force" in app.runner.last.spec.cli and len(list((garmin_tree / "NewFiles").glob("*.fit"))) == 2
            # the watch "imports" them into Workouts
            (garmin_tree / "Workouts").mkdir()
            for f in (garmin_tree / "NewFiles").glob("*.fit"):
                (garmin_tree / "Workouts" / f.name).write_bytes(f.read_bytes())
            # w: watch view, read as a job
            await pilot.press("w")
            await wait_jobs(app)
            await pilot.pause()
            assert screen.view == "watch" and table.row_count == 2
            status = str(screen.query_one("#workouts-status", Panel).content)
            assert "2 of ~25 workouts on the watch" in status
            assert "1 warmup" not in str(screen.query_one("#workout-preview", Panel).content) and "1 active 10:00" in str(screen.query_one("#workout-preview", Panel).content)
            # file actions are inert in the watch view
            await pilot.press("n")
            await pilot.pause()
            assert len(list_workouts(config)) == 1
            # Del twice removes the highlighted one (backup first)
            await pilot.press("delete")
            await pilot.pause()
            assert "Press Delete again to remove Tomorrow from the watch" in str(screen.query_one("#workouts-status", Panel).content)
            await pilot.press("delete")
            await wait_jobs(app)
            await pilot.pause()
            assert table.row_count == 1 and len(list((garmin_tree / "Workouts").glob("*.fit"))) == 1
            assert len(list(backup_dir(config).iterdir())) == 1 and "--yes" in app.runner.last.spec.cli
            # r reads again, w goes back to the files
            await pilot.press("r")
            await wait_jobs(app)
            await pilot.pause()
            assert table.row_count == 1
            await pilot.press("w")
            await pilot.pause()
            assert screen.view == "files" and table.row_count == 1

    run(scenario())


def test_watch_view_without_device(config: Config, tmp_path: Path):
    deps = make_deps(tmp_path)
    app = make_app(config, deps)

    async def scenario():
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("0")
            await pilot.pause()
            await pilot.press("w")
            await wait_jobs(app)
            await pilot.pause()
            status = str(app.screen.query_one("#workouts-status", Panel).content)
            assert "No Garmin device found" in status and "no mounter in tests" in status

    run(scenario())
