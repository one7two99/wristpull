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
from ...workouts.store import list_workouts, load, push_status, save, upcoming, workouts_dir
from ...workouts.watch import WORKOUT_LIMIT, RemoveResult, WatchListing, WatchWorkout, backup_dir
from ..format import fmt_date
from ..i18n import _
from ..jobs import JobRecord, MountOutcome, mount_job, watch_list_job, watch_remove_job, workout_push_job, workout_verify_job
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
        Binding("f", "push_upcoming(True)", "Force push upcoming", show=False),
        Binding("w", "toggle_view", "Files / watch", show=False),
        Binding("r", "read_watch", "Read watch", show=False),
        Binding("m", "mount", "Mount watch", show=False),
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
        self.view = "files"  # files | watch
        self.watch: WatchListing | None = None
        self.watch_selected: str | None = None
        self.confirm_remove: str | None = None

    def screen_title(self) -> str:
        return _("Workouts")

    def keys(self) -> list[tuple[str, str]]:
        if self.view == "watch":
            return [("w", _("files")), ("r", _("read watch")), ("m", _("mount")), ("Del", _("remove from watch")), ("x", _("stop")), ("Esc", _("back"))]
        return [("w", _("watch")), ("m", _("mount")), ("n", _("new")), ("t", _("template")), ("e", _("edit")), ("c", _("drafts from plan")), ("p", _("push")), ("P", _("push upcoming")), ("f", _("force push")), ("d", _("dry run")), ("v", _("verify")), ("Del", _("delete")), ("←→", _("shift period")), ("Esc", _("back"))]

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
        if self.view == "watch":
            self.refresh_watch()
            return
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
        if event.row_key is None or not event.row_key.value:
            return
        key = str(event.row_key.value)
        if self.view == "watch":
            if key != self.watch_selected:
                self.confirm_remove = None
            self.show_watch(key)
            return
        if key != self.selected:
            self.confirm_delete = None
        self.show(key)

    # -- the watch view -----------------------------------------------------------------

    def watch_item(self) -> WatchWorkout | None:
        for item in (self.watch.items if self.watch else []):
            if item.key == self.watch_selected:
                return item
        return None

    def refresh_watch(self) -> None:
        app = self.app
        status: list[str] = []
        if self.watch is None:
            status.append(_("Reading the watch … (r reads it again)"))
        elif self.watch.root is None:
            status.append(_("No Garmin device found. Connect the watch (MTP) or set a path.") + "  " + _("m mounts it again."))
            if self.watch.mount_message:
                status += self.watch.mount_message.splitlines()
        else:
            status.append(_("{active} active, {pending} waiting for unplugging (of ~{limit}) · {root}").format(active=len(self.watch.active), pending=len(self.watch.pending), limit=WORKOUT_LIMIT, root=self.watch.root))
            if any(i.name_clash for i in self.watch.items):
                status.append("⚠ " + _("Workouts with the same name may replace each other on the watch; give them distinct names."))
            if self.watch.near_limit:
                status.append("⚠ " + _("The fenix 6 stores 20-25 workouts; above that it drops new files silently. Delete old ones under GARMIN/Workouts first."))
            status.append(_("Removed files are backed up to {path}; the watch updates its list when unplugged.").format(path=backup_dir(app.config)))
        if app.runner.busy and app.runner.active and app.runner.active.key.startswith("workout_"):
            status.append("⏳ " + _("running: {title}").format(title=app.runner.active.title))
        if self.confirm_remove:
            status.append("⚠ " + _("Press Delete again to remove {name} from the watch").format(name=self.confirm_remove))
        self.query_one("#workouts-status", Panel).set_lines(status)
        table = self.query_one("#workout-table", DataTable)
        table.clear(columns=True)
        table.add_columns(_("Status"), _("Name"), _("Sport"), _("Steps"), _("Created"), _("Origin"))
        items = self.watch.items if self.watch else []
        for item in items:
            created = item.time_created_utc[:10] if item.time_created_utc else "–"
            state = (_("active") if item.location == "active" else _("waiting")) + (" ⚠" if item.name_clash else "")
            table.add_row(state, item.name[:28], item.sport, str(item.steps), created, item.origin if not item.error else "✗", key=item.key)
        if items:
            keys = [i.key for i in items]
            wanted = self.watch_selected if self.watch_selected in keys else keys[0]
            self.show_watch(wanted)
            table.move_cursor(row=keys.index(wanted))
        else:
            self.query_one("#workout-preview", Panel).set_lines([_("No workouts on the watch.") if self.watch and self.watch.root else "–"])

    def show_watch(self, key: str) -> None:
        self.watch_selected = key
        item = self.watch_item()
        panel = self.query_one("#workout-preview", Panel)
        if item is None:
            panel.set_lines(["–"])
            return
        panel.border_title = f"{item.name} · {item.origin} · " + (_("active") if item.location == "active" else _("waiting for unplugging"))
        lines = describe_steps(item.workout) if item.workout else []
        if item.error:
            lines.append("✗ " + item.error)
        lines += ["", f"{item.path.name} · {item.size} B · {item.time_created_utc or '–'}"]
        panel.set_lines(lines)

    def action_toggle_view(self) -> None:
        self.view = "watch" if self.view == "files" else "files"
        self.confirm_delete = self.confirm_remove = None
        self.query_one("#keybar").set_keys(self.keys() + [("/", _("palette")), ("?", _("help")), ("F12", _("language")), ("q", _("quit"))])
        if self.view == "watch" and self.watch is None:
            self.action_read_watch()
            return
        self.refresh_data()

    def action_read_watch(self) -> None:
        if self.view != "watch":
            return
        self.app.start_job(watch_list_job(self.app.config, self.app.deps))
        self.refresh_data()

    # -- actions ------------------------------------------------------------------------

    def action_shift(self, direction: int) -> None:
        if self.view != "files":
            return
        start, end = self.period()
        span = (end - start).days + 1
        self.period_start, self.period_end = start + timedelta(days=direction * span), end + timedelta(days=direction * span)
        self.refresh_data()

    def action_toggle_template(self) -> None:
        if self.view != "files":
            return
        self.template_kind = "distance" if self.template_kind == "time" else "time"
        self.refresh_data()

    def action_new(self) -> None:
        if self.view != "files":
            return
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
        if self.view != "files":
            return
        workout = self.current()
        if workout is None or not workout.path:
            self.app.notify(_("Select a workout first"), severity="warning")
            return
        self.app.edit_file(Path(workout.path))
        self.refresh_data()

    def action_drafts(self) -> None:
        if self.view != "files":
            return
        result = drafts_from_plan(self.app.conn, self.app.config)
        if result.message:
            self.app.notify(result.message, severity="warning")
            return
        self.app.notify(_("{n} workout drafts written, {skipped} days skipped").format(n=len(result.created), skipped=len(result.skipped)))
        if result.created:
            self.selected = str(result.created[0])
        self.refresh_data()

    def _push(self, workouts: list[Workout], *, dry_run: bool = False, force: bool = False) -> None:
        if not workouts:
            self.app.notify(_("Nothing to push"), severity="warning")
            return
        fresh = [load(Path(w.path)) if w.path else w for w in workouts]
        invalid = [w for w in fresh if not w.valid]
        if invalid:
            self.app.notify(_("{n} invalid workouts are skipped").format(n=len(invalid)), severity="warning")
        self.app.start_job(workout_push_job(self.app.config, self.app.deps, fresh, dry_run=dry_run, force=force))
        self.refresh_data()

    def action_push(self) -> None:
        if self.view != "files":
            return
        workout = self.current()
        self._push([workout] if workout else [])

    def action_dry_run(self) -> None:
        if self.view != "files":
            return
        workout = self.current()
        self._push([workout] if workout else [], dry_run=True)

    def action_push_upcoming(self, force: bool = False) -> None:
        if self.view != "files":
            return
        self._push(upcoming(self.app.config, self.app.conn, self.app.deps.today(), force=force), force=force)

    def action_verify(self) -> None:
        if self.view != "files":
            return
        self.app.start_job(workout_verify_job(self.app.config, self.app.deps))
        self.refresh_data()

    def action_delete(self) -> None:
        if self.view == "watch":
            item = self.watch_item()
            if item is None:
                return
            if self.confirm_remove != item.name:
                self.confirm_remove = item.name
                self.refresh_data()
                return
            self.confirm_remove = None
            self.app.start_job(watch_remove_job(self.app.config, self.app.deps, [item.key]))
            self.refresh_data()
            return
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

    def action_mount(self) -> None:
        self.app.start_job(mount_job(self.app.config, self.app.deps))
        self.refresh_data()

    def job_finished(self, record: JobRecord) -> None:
        if isinstance(record.result, MountOutcome):
            self.app.notify(record.lines[0] if record.lines else "", severity="information" if record.result.mounted else "error")
            if record.result.mounted and self.view == "watch":
                self.app.start_job(watch_list_job(self.app.config, self.app.deps))
            self.refresh_data()
            return
        if isinstance(record.result, WatchListing):
            self.watch = record.result
        elif isinstance(record.result, RemoveResult) and record.result.listing is not None:
            self.watch = record.result.listing
        self.refresh_data()

    def job_line(self, text: str) -> None:
        pass
