"""Screen 9: the training coach - run it, read and rate the answers, maintain the goal profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input

from ...coach.profile import apply_wizard, load_profile, profile_path
from ...coach.runner import CoachResult, resolve_language
from ...coach.store import add_feedback, add_note, get_run, recent_notes, recent_runs
from ...workouts.drafts import drafts_from_plan
from ..format import fmt_datetime, fmt_float, fmt_int
from ..i18n import _
from ..jobs import JobRecord, coach_job
from ..widgets import Panel
from .base import WristpullScreen
from .coach_wizard import WizardScreen

RATING_MARK = {1: "👍", 0: "😐", -1: "👎"}


class CoachScreen(WristpullScreen):
    NAME = "coach"
    BINDINGS = [
        Binding("d", "run('daily')", "Daily", show=False),
        Binding("w", "run('weekly')", "Weekly", show=False),
        Binding("p", "dry_run", "Dry run", show=False),
        Binding("x", "app.cancel_job", "Stop", show=False),
        Binding("n", "note_mode", "Journal note", show=False),
        Binding("g", "wizard", "Goal wizard", show=False),
        Binding("e", "edit_profile", "Edit profile", show=False),
        Binding("t", "draft_workouts", "Draft workouts", show=False),
        Binding("plus", "rate(1)", "Good", show=False),
        Binding("minus", "rate(-1)", "Bad", show=False),
        Binding("0", "rate(0)", "Neutral", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_run: int | None = None
        self.input_mode = "ask"

    def screen_title(self) -> str:
        return _("Coach")

    def keys(self) -> list[tuple[str, str]]:
        return [("d", _("daily")), ("w", _("weekly")), ("Enter", _("ask")), ("n", _("journal note")), ("p", _("dry run")), ("x", _("stop")), ("+ − 0", _("rate")), ("g", _("goal wizard")), ("e", _("edit profile")), ("t", _("draft workouts")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Vertical(id="coach"):
            yield Panel(_("Coach"), id="coach-status")
            with Horizontal(classes="row grow"):
                yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="answer-scroll"):
                    yield Panel(_("Answer"), id="coach-answer")
            yield Input(placeholder=_("Ask the coach a question and press Enter …"), id="question")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#runs", DataTable).focus()

    # -- data ---------------------------------------------------------------------------

    def refresh_data(self) -> None:
        app = self.app
        lang = resolve_language(app.config, None)
        _text, _sha, placeholders = load_profile(app.config, lang)
        coach = app.config.coach
        status = [
            _("Backend {backend} · model {model}").format(backend=coach.backend, model=coach.model) + (f" · {_('weekly')} {coach.weekly_model}" if coach.weekly_model else ""),
            _("Profile {path}").format(path=profile_path(app.config)) + ("  ✓" if not placeholders else "  ✗ " + _("{n} open placeholders (g for the wizard, e to edit)").format(n=len(placeholders))),
        ]
        if app.runner.busy and app.runner.active and app.runner.active.key.startswith("coach_"):
            status.append("⏳ " + _("running: {title}").format(title=app.runner.active.title))
        latest = recent_notes(app.conn, 1)
        if latest:
            status.append(_("Journal {date}: {note}").format(date=latest[0]["local_date"], note=latest[0]["note"][:80]))
        self.query_one("#coach-status", Panel).set_lines(status)
        runs = recent_runs(app.conn, limit=50, status="ok") + recent_runs(app.conn, limit=10, status="dry_run") + recent_runs(app.conn, limit=10, status="error")
        runs.sort(key=lambda r: r["id"], reverse=True)
        table = self.query_one("#runs", DataTable)
        table.clear(columns=True)
        table.add_columns("#", _("Kind"), _("When"), _("Model"), _("Tokens"), _("Cost"), _("Rating"), _("Status"))
        for run in runs:
            tokens = f"{fmt_int(run.get('input_tokens'))}/{fmt_int(run.get('output_tokens'))}" if run.get("input_tokens") else "–"
            cost = f"${run['cost_usd']:.4f}" if run.get("cost_usd") else "–"
            rating = RATING_MARK.get(run.get("rating"), "–") if run.get("rating") is not None else "–"
            table.add_row(str(run["id"]), run["kind"], fmt_datetime(run["created_at_utc"]), run["model"][:28], tokens, cost, rating, run["status"], key=str(run["id"]))
        if runs:
            wanted = str(self.selected_run) if self.selected_run and any(str(r["id"]) == str(self.selected_run) for r in runs) else str(runs[0]["id"])
            self.show_run(wanted)
        else:
            self.query_one("#coach-answer", Panel).set_lines([_("No coach runs yet. Press d for the daily check or type a question below.")])

    def show_run(self, key: str) -> None:
        run = get_run(self.app.conn, int(key))
        panel = self.query_one("#coach-answer", Panel)
        if run is None:
            panel.set_lines(["–"])
            return
        self.selected_run = int(key)
        head = f"#{run['id']} {run['kind']} · {fmt_datetime(run['created_at_utc'])} · {run['backend']}/{run['model']}"
        if run.get("duration_s"):
            head += f" · {fmt_float(run['duration_s'], 0)} s"
        panel.border_title = head
        if run["status"] == "dry_run":
            panel.set_lines([_("Dry run: the prompt was written to the coach/dryrun folder; nothing was sent.")])
        elif run["status"] == "error":
            panel.set_lines(["✗ " + str(run.get("error") or _("failed"))])
        else:
            panel.set_lines((run.get("answer_md") or "").splitlines())

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.show_run(str(event.row_key.value))

    # -- actions ------------------------------------------------------------------------

    def _start(self, kind: str, *, question: str | None = None, dry_run: bool = False) -> None:
        app = self.app
        self.app.start_job(coach_job(app.config, app.deps, kind, today=app.deps.today(), question=question, dry_run=dry_run))
        self.refresh_data()

    def action_run(self, kind: str) -> None:
        self._start(kind)

    def action_dry_run(self) -> None:
        self._start("daily", dry_run=True)

    @on(Input.Submitted, "#question")
    def _ask(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            self.app.notify(_("Type a question first") if self.input_mode == "ask" else _("Type a note first"), severity="warning")
            return
        event.input.value = ""
        if self.input_mode == "note":
            add_note(self.app.conn, self.app.deps.today().isoformat(), text)
            self.app.notify(_("Noted for today"))
            self.action_note_mode()  # back to asking
            self.refresh_data()
        else:
            self._start("ask", question=text)
        self.query_one("#runs", DataTable).focus()

    def action_note_mode(self) -> None:
        """Toggle the input between asking the coach and writing today's journal line."""
        box = self.query_one("#question", Input)
        if self.input_mode == "ask":
            self.input_mode = "note"
            box.placeholder = _("Journal for today (how you feel, what you did) and press Enter …")
            box.focus()
        else:
            self.input_mode = "ask"
            box.placeholder = _("Ask the coach a question and press Enter …")

    def action_rate(self, rating: int) -> None:
        if self.selected_run is None:
            self.app.notify(_("Select a run first"), severity="warning")
            return
        try:
            add_feedback(self.app.conn, self.selected_run, rating)
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.app.notify(_("Rated run #{id}: {mark}").format(id=self.selected_run, mark=RATING_MARK[rating]))
        self.refresh_data()

    def action_wizard(self) -> None:
        lang = resolve_language(self.app.config, None)

        def done(answers: dict[str, Any] | None) -> None:
            if answers is None:
                return
            path = apply_wizard(self.app.config, answers, lang)
            self.app.notify(_("Profile written: {path}").format(path=path))
            self.refresh_data()

        self.app.push_screen(WizardScreen(self.app.config, lang), done)

    def action_edit_profile(self) -> None:
        path = profile_path(self.app.config)
        load_profile(self.app.config, resolve_language(self.app.config, None))  # creates the template on first use
        self.app.edit_file(path)
        self.refresh_data()

    def action_draft_workouts(self) -> None:
        """One draft workout file per session of the last weekly plan, then switch to the workouts screen."""
        result = drafts_from_plan(self.app.conn, self.app.config)
        if result.message:
            self.app.notify(result.message, severity="warning")
            return
        self.app.notify(_("{n} workout drafts written, {skipped} days skipped").format(n=len(result.created), skipped=len(result.skipped)))
        self.app.action_goto("workouts")

    def job_finished(self, record: JobRecord) -> None:
        if isinstance(record.result, CoachResult):
            self.selected_run = record.result.run_id
        self.refresh_data()

    def job_line(self, text: str) -> None:  # the status panel is enough here; the log lives on the sync screen
        pass


def profile_file(config) -> Path:
    return profile_path(config)
