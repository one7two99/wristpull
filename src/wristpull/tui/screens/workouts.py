"""Screen 0: structured workouts - write them as Markdown, push them to the watch, see what it took over."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable

from ...garmin_usb import find_garmin_root
from ...workouts.drafts import drafts_from_plan
from ...workouts.model import Workout, describe_steps, template
from ...workouts.store import list_workouts, load, push_status, save, workouts_dir
from ..format import fmt_date
from ..i18n import _
from ..jobs import JobRecord, workout_push_job, workout_verify_job
from ..widgets import Panel
from .base import WristpullScreen

PAST_DAYS = 7
FUTURE_DAYS = 21


class WorkoutsScreen(WristpullScreen):
    NAME = "workouts"
    BINDINGS = [
        Binding("n", "new", "New", show=False),
        Binding("t", "toggle_template", "Template", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("c", "drafts", "Drafts from plan", show=False),
        Binding("p", "push", "Push", show=False),
        Binding("P", "push_upcoming", "Push upcoming", show=False),
        Binding("d", "dry_run", "Dry run", show=False),
        Binding("v", "verify", "Verify", show=False),
        Binding("delete", "delete", "Delete", show=False),
        Binding("left", "shift(-1)", "Earlier", show=False),
        Binding("right", "shift(1)", "Later", show=False),
        Binding("x", "app.cancel_job", "Stop", show=False),
        Binding("escape", "app.goto('home')", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.template_kind = "time"
        self.selected: str | None = None
        self.period_start: date | None = None
        self.period_end: date | None = None
        self.confirm_delete: str | None = None
        self.workouts: list[Workout] = []

    def screen_title(self) -> str:
        return _("Workouts")

    def keys(self) -> list[tuple[str, str]]:
        return [("n", _("new")), ("t", _("template")), ("e", _("edit")), ("c", _("drafts from plan")), ("p", _("push")), ("P", _("push upcoming")), ("d", _("dry run")), ("v", _("verify")), ("Del", _("delete")), ("←→", _("shift period")), ("Esc", _("back"))]

    def body(self) -> ComposeResult:
        with Vertical(id="workouts"):
            yield Panel(_("Workouts"), id="workouts-status")
            with Horizontal(classes="row grow"):
                yield DataTable(id="workout-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="preview-scroll"):
                    yield Panel(_("Preview"), id="workout-preview")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#workout-table", DataTable).focus()

    # -- data ---------------------------------------------------------------------------

    def period(self) -> tuple[date, date]:
        if self.period_start is None or self.period_end is None:
            today = self.app.deps.today()
            self.period_start, self.period_end = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
        return self.period_start, self.period_end

    def state_of(self, workout: Workout) -> str:
        if not workout.valid:
            return "invalid"
        return push_status(self.app.conn, workout.path or "") or workout.status

    def refresh_data(self) -> None:
        app = self.app
        start, end = self.period()
        self.workouts = list_workouts(app.config, start=start, end=end)
        roots = app.deps.roots() if app.deps.roots else []
        connected = find_garmin_root(roots) is not None if roots else False
        states = [self.state_of(w) for w in self.workouts]
        status = [
            _("Folder {path} · {start} – {end}").format(path=workouts_dir(app.config), start=fmt_date(start, year=True), end=fmt_date(end, year=True)),
            (_("Watch connected") if connected else _("No watch mounted (push mounts it when possible)")) + " · " + _("{drafts} drafts, {pushed} pushed, {verified} verified, {invalid} invalid").format(drafts=states.count("draft") + states.count("ready"), pushed=states.count("pushed"), verified=states.count("verified"), invalid=states.count("invalid")),
            _("Template for n: {kind}").format(kind=_("time") if self.template_kind == "time" else _("distance")) + " · " + _("target folder {folder}").format(folder=app.config.workout.folder),
        ]
        if app.runner.busy and app.runner.active and app.runner.active.key.startswith("workout_"):
            status.append("⏳ " + _("running: {title}").format(title=app.runner.active.title))
        if self.confirm_delete:
            status.append("⚠ " + _("Press Delete again to remove {path}").format(path=Path(self.confirm_delete).name))
        self.query_one("#workouts-status", Panel).set_lines(status)
        table = self.query_one("#workout-table", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Date"), _("Name"), _("Steps"), _("Total"), _("Status"))
        for workout, state in zip(self.workouts, states):
            total = f"{workout.total_seconds // 60} min" + (f" + {workout.total_metres / 1000:g} km" if workout.total_metres else "")
            table.add_row(workout.day.isoformat() if workout.day else "–", workout.name[:28], str(len(workout.steps)), total, state, key=workout.path or workout.name)
        if self.workouts:
            keys = [w.path or w.name for w in self.workouts]
            wanted = self.selected if self.selected in keys else keys[0]
            self.show(wanted)
            try:
                table.move_cursor(row=keys.index(wanted))
            except ValueError:
                pass
        else:
            self.query_one("#workout-preview", Panel).set_lines([_("No workouts in this period. Press n for a new one or c for drafts from the coach's weekly plan.")])

    def current(self) -> Workout | None:
        for workout in self.workouts:
            if (workout.path or workout.name) == self.selected:
                return workout
        return None

    def show(self, key: str) -> None:
        self.selected = key
        workout = self.current()
        panel = self.query_one("#workout-preview", Panel)
        if workout is None:
            panel.set_lines(["–"])
            return
        panel.border_title = f"{workout.name} · {workout.day.isoformat() if workout.day else '–'} · {self.state_of(workout)}"
        lines = describe_steps(workout)
        for error in workout.errors:
            lines.append("✗ " + error)
        if workout.path:
            lines += ["", "─" * 40, ""]
            try:
                lines += Path(workout.path).read_text("utf-8").splitlines()
            except OSError as exc:
                lines.append(str(exc))
        panel.set_lines(lines)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            if str(event.row_key.value) != self.selected:
                self.confirm_delete = None
            self.show(str(event.row_key.value))

    # -- actions ------------------------------------------------------------------------

    def action_shift(self, direction: int) -> None:
        start, end = self.period()
        span = (end - start).days + 1
        self.period_start, self.period_end = start + timedelta(days=direction * span), end + timedelta(days=direction * span)
        self.refresh_data()

    def action_toggle_template(self) -> None:
        self.template_kind = "distance" if self.template_kind == "time" else "time"
        self.refresh_data()

    def action_new(self) -> None:
        app = self.app
        day = app.deps.today() + timedelta(days=1)
        workout = template(self.template_kind, day, app.config.workout.defaults())
        for _attempt in range(10):
            try:
                path = save(app.config, workout)
                break
            except FileExistsError:
                workout.name = workout.name[:26] + " " + str(_attempt + 2)
        else:
            app.notify(_("Could not find a free file name"), severity="error")
            return
        self.selected = str(path)
        app.edit_file(path)
        self.refresh_data()

    def action_edit(self) -> None:
        workout = self.current()
        if workout is None or not workout.path:
            self.app.notify(_("Select a workout first"), severity="warning")
            return
        self.app.edit_file(Path(workout.path))
        self.refresh_data()

    def action_drafts(self) -> None:
        result = drafts_from_plan(self.app.conn, self.app.config)
        if result.message:
            self.app.notify(result.message, severity="warning")
            return
        self.app.notify(_("{n} workout drafts written, {skipped} days skipped").format(n=len(result.created), skipped=len(result.skipped)))
        if result.created:
            self.selected = str(result.created[0])
        self.refresh_data()

    def _push(self, workouts: list[Workout], *, dry_run: bool = False) -> None:
        if not workouts:
            self.app.notify(_("Nothing to push"), severity="warning")
            return
        fresh = [load(Path(w.path)) if w.path else w for w in workouts]
        invalid = [w for w in fresh if not w.valid]
        if invalid:
            self.app.notify(_("{n} invalid workouts are skipped").format(n=len(invalid)), severity="warning")
        self.app.start_job(workout_push_job(self.app.config, self.app.deps, fresh, dry_run=dry_run))
        self.refresh_data()

    def action_push(self) -> None:
        workout = self.current()
        self._push([workout] if workout else [])

    def action_dry_run(self) -> None:
        workout = self.current()
        self._push([workout] if workout else [], dry_run=True)

    def action_push_upcoming(self) -> None:
        today = self.app.deps.today()
        upcoming = [w for w in self.workouts if w.valid and w.day is not None and w.day >= today and self.state_of(w) not in ("pushed", "verified")]
        self._push(upcoming)

    def action_verify(self) -> None:
        self.app.start_job(workout_verify_job(self.app.config, self.app.deps))
        self.refresh_data()

    def action_delete(self) -> None:
        workout = self.current()
        if workout is None or not workout.path:
            return
        if self.confirm_delete != workout.path:
            self.confirm_delete = workout.path
            self.refresh_data()
            return
        Path(workout.path).unlink(missing_ok=True)
        self.confirm_delete = None
        self.selected = None
        self.app.notify(_("Deleted {path}").format(path=Path(workout.path).name))
        self.refresh_data()

    def job_finished(self, record: JobRecord) -> None:
        self.refresh_data()

    def job_line(self, text: str) -> None:
        pass
