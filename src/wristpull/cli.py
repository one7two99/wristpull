"""Command-line interface: wristpull import garmin | status | inspect | config | ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, default_config_path, load_config, write_default_config
from .db import connect
from .fit.decode import decode_fit, summarize
from .adb import AdbError
from .importers.garmin import import_garmin
from .importers.phone import import_gadgetbridge_db, import_gadgetbridge_fit_dir, pull_phone
from .status import collect_status, render_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wristpull", description="Collect wearable health data locally.")
    parser.add_argument("--version", action="version", version=f"wristpull {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import data from a device")
    import_sub = p_import.add_subparsers(dest="device", required=True)
    p_garmin = import_sub.add_parser("garmin", help="import FIT files from a USB-connected Garmin watch")
    p_garmin.add_argument("--path", type=Path, help="GARMIN folder or mount point (default: auto-detect)")
    p_garmin.add_argument("--dry-run", action="store_true", help="list new files without copying or parsing")
    p_gb = import_sub.add_parser("gadgetbridge", help="import an already copied Gadgetbridge export (database and/or files dir)")
    p_gb.add_argument("--db", type=Path, help="exported Gadgetbridge.db")
    p_gb.add_argument("--files", type=Path, help="local copy of Android/data/nodomain.freeyourgadget.gadgetbridge/files")

    sub.add_parser("status", help="show what is stored and whether a device is connected")

    p_inspect = sub.add_parser("inspect", help="dump message types and fields of a FIT file")
    p_inspect.add_argument("file", type=Path)
    p_inspect.add_argument("--messages", help="comma-separated message names/numbers to show in full")
    p_inspect.add_argument("--limit", type=int, default=5, help="rows per message type when --messages is used")

    p_mount = sub.add_parser("mount", help="mount the USB-connected Garmin watch (releases KDE's hold on the MTP device if needed)")
    p_mount.add_argument("--remount", action="store_true", help="unmount a stale mount first, then mount again")

    p_config = sub.add_parser("config", help="show or create the configuration file")
    p_config.add_argument("--init", action="store_true", help="write a default config file if none exists")

    p_pull = sub.add_parser("pull", help="pull Gadgetbridge data from the phone over ADB")
    p_pull.add_argument("what", nargs="?", default="phone", choices=["phone"])
    p_pull.add_argument("--serial", help="ADB serial (default: [phone] serial from the config, or the only device)")
    p_pull.add_argument("--no-trigger", action="store_true", help="do not ask Gadgetbridge to sync and export first")
    p_pull.add_argument("--no-sync", action="store_true", help="trigger the export but not a device sync")
    p_pull.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the export to finish (default 60)")
    p_pull.add_argument("--dry-run", action="store_true", help="show what would be pulled")
    p_export = sub.add_parser("export", help="export a compact Markdown summary plus CSVs for a date range")
    p_export.add_argument("--for-claude", action="store_true", help="compact Markdown + CSV attachments (the only format so far)")
    p_export.add_argument("--from", dest="date_from", required=True, help="first local date, YYYY-MM-DD")
    p_export.add_argument("--to", dest="date_to", required=True, help="last local date, YYYY-MM-DD")
    p_export.add_argument("--out", type=Path, help="output directory (default <data_dir>/exports/<from>_<to>)")
    p_export.add_argument("--no-csv", action="store_true", help="write only summary.md")
    p_export.add_argument("--stdout", action="store_true", help="also print the Markdown")

    p_report = sub.add_parser("report", help="render a weekly or monthly Markdown report")
    p_report.add_argument("period", choices=["week", "month"])
    p_report.add_argument("--date", help="a date inside the week/month (default: today)")
    p_report.add_argument("--charts", action="store_true", help="also write SVG charts next to the report")
    p_report.add_argument("--out", type=Path, help="output directory (default <data_dir>/reports/<period>-<label>)")
    p_report.add_argument("--stdout", action="store_true", help="also print the Markdown")

    sub.add_parser("tui", help="open the terminal UI (requires the 'tui' extra: uv sync --extra tui)")

    p_coach = sub.add_parser("coach", help="training coach: send aggregated data to an LLM and store the answer")
    coach_sub = p_coach.add_subparsers(dest="coach_cmd", required=True)
    for name, help_text in (
        ("daily", "morning check: recovery and a suggestion for today"),
        ("weekly", "review of the last completed week (the current one on a Sunday) with a plan for the next"),
        ("ask", "a free question over the daily data package"),
    ):
        p_c = coach_sub.add_parser(name, help=help_text)
        if name == "ask":
            p_c.add_argument("question")
        p_c.add_argument("--backend", choices=["ollama", "claude", "openrouter"], help="override [coach] backend")
        p_c.add_argument("--model", help="override [coach] model")
        p_c.add_argument("--lang", choices=["de", "en"], help="answer language (default: [coach] language or the UI language)")
        p_c.add_argument("--date", help="treat this local date as today (YYYY-MM-DD)")
        p_c.add_argument("--dry-run", action="store_true", help="write the prompt to <data_dir>/coach/dryrun/ instead of calling the model")
        p_c.add_argument("--force", action="store_true", help="run although the goal profile still has placeholders")
    p_show = coach_sub.add_parser("show", help="print stored coach answers")
    p_show.add_argument("--last", type=int, default=1, help="how many runs (default 1)")
    p_show.add_argument("--kind", choices=["daily", "weekly", "ask"])
    p_profile = coach_sub.add_parser("profile", help="show the goal profile (path, open placeholders) or run the goal wizard")
    p_profile.add_argument("--wizard", action="store_true", help="answer multiple-choice questions and (re)generate the profile's wizard block")
    p_profile.add_argument("--lang", choices=["de", "en"], help="language of the generated profile text")
    p_note = coach_sub.add_parser("note", help="write a one-line journal entry the coach reads (how you felt, what you did)")
    p_note.add_argument("text")
    p_note.add_argument("--date", help="local date (default today)")
    p_notes = coach_sub.add_parser("notes", help="print recent journal entries")
    p_notes.add_argument("--last", type=int, default=14)
    p_ics = coach_sub.add_parser("plan-ics", help="write the last weekly plan as an iCalendar file")
    p_ics.add_argument("--out", type=Path, help="target file (default <data_dir>/coach/plan-<week>.ics)")
    p_ics.add_argument("--hour", type=int, default=7, help="start hour of timed sessions (default 7)")
    p_rate = coach_sub.add_parser("rate", help="rate a stored run: -1 (not useful), 0, 1 (useful); the coach sees it next time")
    p_rate.add_argument("run_id", type=int)
    p_rate.add_argument("rating", type=int, choices=[-1, 0, 1])
    p_rate.add_argument("--note", help="optional comment, e.g. what you actually did")

    p_workout = sub.add_parser("workout", help="structured workouts: write them as Markdown, push them to the watch as FIT files")
    workout_sub = p_workout.add_subparsers(dest="workout_cmd", required=True)
    p_wnew = workout_sub.add_parser("new", help="create a workout file from a template and print its path")
    p_wnew.add_argument("--date", help="planned local date (default: tomorrow)")
    p_wnew.add_argument("--name", help="workout name (max 30 characters)")
    p_wnew.add_argument("--template", choices=["time", "distance", "intervals"], default="time", help="main part by time (default), by distance, or an interval block (6 x 400 m) to edit")
    p_wnew.add_argument("--minutes", type=int, default=40, help="main part in minutes for --template time (default 40)")
    p_wnew.add_argument("--metres", type=int, default=5000, help="main part in metres for --template distance (default 5000)")
    p_wnew.add_argument("--edit", action="store_true", help="open the new file in $EDITOR")
    p_wlist = workout_sub.add_parser("list", help="list the workout files with their status")
    p_wlist.add_argument("--from", dest="date_from", help="first local date")
    p_wlist.add_argument("--to", dest="date_to", help="last local date")
    p_wcheck = workout_sub.add_parser("check", help="parse and validate workout files, show the steps as the watch will see them")
    p_wcheck.add_argument("files", nargs="+", type=Path)
    p_wpush = workout_sub.add_parser("push", help="build FIT files and copy them to the connected watch")
    p_wpush.add_argument("files", nargs="*", type=Path, help="workout files (default: --date or --upcoming)")
    p_wpush.add_argument("--date", help="push the workouts planned for this local date")
    p_wpush.add_argument("--upcoming", action="store_true", help="push every valid workout from today on that is not on the watch yet")
    p_wpush.add_argument("--force", action="store_true", help="also push workouts already pushed (with --upcoming), and write files again even if an identical one is on the watch")
    p_wpush.add_argument("--path", type=Path, help="GARMIN folder or mount point (default: auto-detect)")
    p_wpush.add_argument("--folder", choices=["NewFiles", "Workouts"], help="target folder below GARMIN/ (default: [workout] folder)")
    p_wpush.add_argument("--schedule", action="store_true", help="also write a schedule file with the planned dates")
    p_wpush.add_argument("--dry-run", action="store_true", help="write the FIT files to <data_dir>/workouts/out/ instead of the watch")
    eject_group = p_wpush.add_mutually_exclusive_group()
    eject_group.add_argument("--eject", dest="eject", action="store_true", default=None, help="unmount the MTP mount after writing")
    eject_group.add_argument("--no-eject", dest="eject", action="store_false", help="leave the mount in place (default: [workout] eject)")
    p_wverify = workout_sub.add_parser("verify", help="read GARMIN/Workouts on the watch and mark the pushed workouts it has taken over")
    p_wverify.add_argument("--path", type=Path, help="GARMIN folder or mount point (default: auto-detect)")
    p_wwatch = workout_sub.add_parser("watch", help="list the workouts on the watch: active (GARMIN/Workouts) and pending (GARMIN/NewFiles)")
    p_wwatch.add_argument("--path", type=Path, help="GARMIN folder or mount point (default: auto-detect)")
    p_wremove = workout_sub.add_parser("remove", help="remove workouts from the watch (backed up to <data_dir>/workouts/watch-backup/ first)")
    p_wremove.add_argument("names", nargs="+", help="workout name or file name as shown by 'workout watch' (pending files in NewFiles too)")
    p_wremove.add_argument("--path", type=Path, help="GARMIN folder or mount point (default: auto-detect)")
    p_wremove.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_wplan = workout_sub.add_parser("from-plan", help="draft one workout per session of the coach's last weekly plan")
    p_wplan.add_argument("--week", help="Monday of the week to plan (default: the week the plan was written for)")

    p_reparse = sub.add_parser("reparse", help="re-parse archived raw files after a parser change (rows are replaced)")
    p_reparse.add_argument("--id", type=int, action="append", dest="ids", help="raw_files.id (repeatable)")
    p_reparse.add_argument("--source", help="only this source, e.g. gadgetbridge or garmin_usb")
    p_reparse.add_argument("--kind", help="only this kind, e.g. sleep, monitor, database")
    p_reparse.add_argument("--errors", action="store_true", help="only files whose last parse failed")
    p_reparse.add_argument("--all", action="store_true", help="everything (required when no other filter is given)")
    return parser


def _open(config: Config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return connect(config.db_path)


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"


def cmd_import_gadgetbridge(args: argparse.Namespace, config: Config) -> int:
    if not args.db and not args.files:
        print("pass --db and/or --files", file=sys.stderr)
        return 2
    conn = _open(config)
    log = (lambda _m: None) if args.quiet else print
    total: dict[str, int] = {}
    errors = 0
    if args.db:
        is_new, counts, labels = import_gadgetbridge_db(conn, config, args.db, origin=str(args.db), log=log)
        for label in labels:
            print(f"  device: {label}")
        print(f"{args.db}: {'imported' if is_new else 'already archived'}, inserted: {_fmt(counts)}")
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    if args.files:
        found, new, skipped, errs, counts = import_gadgetbridge_fit_dir(conn, config, args.files, log=log)
        errors += errs
        print(f"{args.files}: {found} FIT files, {new} new, {skipped} already archived, {errs} errors; inserted: {_fmt(counts)}")
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
    if total:
        from .daily import rebuild_daily_summary
        from .gadgetbridge import SOURCE_GARMIN, SOURCE_ZEPP

        for source in (SOURCE_ZEPP, SOURCE_GARMIN):
            rebuild_daily_summary(conn, source=source)
        conn.commit()
    return 0 if not errors else 2


def cmd_pull(args: argparse.Namespace, config: Config) -> int:
    conn = _open(config)
    log = (lambda _m: None) if args.quiet else print
    if args.serial:
        config.phone_serial = args.serial
    try:
        stats = pull_phone(
            conn,
            config,
            trigger=not args.no_trigger,
            sync=not args.no_sync,
            wait=args.wait,
            dry_run=args.dry_run,
            log=log,
        )
    except AdbError as exc:
        print(f"adb: {exc}", file=sys.stderr)
        return 1
    mode = "dry run: " if args.dry_run else ""
    db_state = "not found" if not stats.db_pulled else ("new export imported" if stats.db_new else "unchanged")
    print(f"{mode}phone {stats.device}: database {db_state}; {stats.fit_found} Garmin FIT files, {stats.fit_new} new, {stats.fit_skipped} already archived, {stats.errors} errors")
    for label in stats.db_devices:
        print(f"  device: {label}")
    if stats.rows:
        print("inserted: " + _fmt(stats.rows))
    for message in stats.messages:
        print(f"  note: {message}", file=sys.stderr)
    return 0 if not stats.errors else 2


def cmd_import_garmin(args: argparse.Namespace, config: Config) -> int:
    conn = _open(config)
    log = (lambda _m: None) if args.quiet else print
    stats = import_garmin(conn, config, path=args.path, dry_run=args.dry_run, log=log)
    if stats.root is None:
        print("No Garmin device found. Connect the watch (MTP) or pass --path <GARMIN folder>.", file=sys.stderr)
        if stats.mount_message:
            print(stats.mount_message, file=sys.stderr)
        return 1
    mode = "dry run: " if args.dry_run else ""
    print(f"{mode}{stats.root}: {stats.found} FIT files, {stats.new} new, {stats.skipped} already archived, {stats.errors} errors")
    if stats.rows:
        print("inserted: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.rows.items())))
    for path, err in stats.error_files:
        print(f"  error {path}: {err}", file=sys.stderr)
    return 0 if not stats.errors else 2


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    conn = _open(config)
    print(render_status(collect_status(conn, config)))
    return 0


def cmd_inspect(args: argparse.Namespace, config: Config) -> int:
    fit = decode_fit(args.file)
    print(f"{args.file}: type={fit.file_type} created={fit.time_created_iso} manufacturer={fit.manufacturer} product={fit.product_name or fit.product} serial={fit.serial_number}")
    if fit.warnings:
        print(f"  {len(fit.warnings)} decoder warning(s); first: {fit.warnings[0]}")
    summary = summarize(fit)
    print(f"{len(fit.messages)} messages in {len(summary)} types:")
    for name, entry in summary.items():
        print(f"  {name} (#{entry['num']}): {entry['count']}")
        for key, examples in entry["fields"].items():
            shown = ", ".join(str(v) for v in examples) if examples else "-"
            print(f"      {key}: {shown}")
    if args.messages:
        wanted = {w.strip() for w in args.messages.split(",") if w.strip()}
        for msg in fit.messages:
            if msg.name in wanted or str(msg.num) in wanted:
                wanted_key = msg.name if msg.name in wanted else str(msg.num)
                count = summary[msg.name].setdefault("_shown", 0)
                if count >= args.limit:
                    continue
                summary[msg.name]["_shown"] = count + 1
                print(f"  [{wanted_key}] " + ", ".join(f"{k}={v}" for k, v in msg.named.items() if v is not None))
    return 0


def cmd_mount(args: argparse.Namespace, config: Config) -> int:
    from .garmin_usb import candidate_roots, find_garmin_root, remount_garmin, try_mount_garmin

    roots = candidate_roots(extra=config.garmin_mount_hints)
    if not args.remount and (root := find_garmin_root(roots)) is not None:
        print(f"already mounted: {root}")
        return 0
    mounted, message = remount_garmin(roots) if args.remount else try_mount_garmin()
    print(message, file=sys.stdout if mounted else sys.stderr)
    if mounted:
        root = find_garmin_root(candidate_roots(extra=config.garmin_mount_hints))
        print(f"GARMIN folder: {root}" if root else "mounted, but no GARMIN folder found yet; try again in a few seconds")
    return 0 if mounted else 1


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    path = args.config or default_config_path()
    if args.init:
        if path.exists():
            print(f"config already exists: {path}")
        else:
            write_default_config(path)
            print(f"wrote default config to {path}")
        return 0
    print(f"# config file: {path} ({'exists' if path.exists() else 'missing, showing defaults'})")
    print(config.render(), end="")
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    from .aggregate import parse_date
    from .export import export_for_claude

    try:
        start, end = parse_date(args.date_from), parse_date(args.date_to)
    except ValueError as exc:
        print(f"invalid date: {exc}", file=sys.stderr)
        return 2
    if end < start:
        print("--to must not be before --from", file=sys.stderr)
        return 2
    conn = _open(config)
    result = export_for_claude(conn, config, start.isoformat(), end.isoformat(), out_dir=args.out, write_csv=not args.no_csv)
    if args.stdout:
        print(result.markdown)
    size = sum(p.stat().st_size for p in result.files)
    print(f"wrote {len(result.files)} files ({size // 1024} KiB) to {result.directory}")
    for path in result.files:
        print(f"  {path.name}")
    return 0


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    from datetime import date

    from .aggregate import parse_date
    from .report import write_report

    try:
        day = parse_date(args.date) if args.date else date.today()
    except ValueError as exc:
        print(f"invalid date: {exc}", file=sys.stderr)
        return 2
    conn = _open(config)
    result = write_report(conn, config, args.period, day, charts=args.charts, out_dir=args.out)
    if args.stdout:
        print(result.markdown)
    print(f"{args.period} {result.start} to {result.end}: wrote {len(result.files)} files to {result.directory}")
    return 0


def cmd_reparse(args: argparse.Namespace, config: Config) -> int:
    from .importers.reparse import reparse

    if not (args.ids or args.source or args.kind or args.errors or args.all):
        print("pass --id, --source, --kind, --errors or --all", file=sys.stderr)
        return 2
    conn = _open(config)
    log = (lambda _m: None) if args.quiet else print
    stats = reparse(conn, config, ids=args.ids, source=args.source, kind=args.kind, errors_only=args.errors, log=log)
    print(f"{stats.selected} files selected, {stats.ok} re-parsed, {stats.errors} errors; removed {stats.deleted} rows, inserted: {_fmt(stats.rows)}")
    if stats.sources:
        print("daily summaries rebuilt for: " + ", ".join(sorted(stats.sources)))
    return 0 if not stats.errors else 2


def cmd_coach(args: argparse.Namespace, config: Config) -> int:
    from .coach.backends import CoachError, make_backend
    from .coach.profile import load_profile
    from .coach.runner import resolve_language, run_coach
    from .coach.store import recent_runs

    conn = _open(config)
    if args.coach_cmd == "show":
        runs = recent_runs(conn, kind=args.kind, limit=args.last)
        if not runs:
            print("no coach runs stored yet")
            return 1
        for run in runs:
            cost = f", ${run['cost_usd']:.4f}" if run.get("cost_usd") else ""
            rating = {1: " 👍", 0: " 😐", -1: " 👎"}.get(run.get("rating"), "") if run.get("rating") is not None else ""
            print(f"--- #{run['id']} {run['kind']} {run['created_at_utc']} {run['backend']}/{run['model']} ({run['input_tokens'] or '?'} in / {run['output_tokens'] or '?'} out{cost}){rating}")
            print(run.get("answer_md") or "")
        return 0
    if args.coach_cmd == "note":
        from datetime import date

        from .aggregate import parse_date
        from .coach.store import add_note

        try:
            day = parse_date(args.date) if args.date else date.today()
            add_note(conn, day.isoformat(), args.text)
        except ValueError as exc:
            print(f"coach: {exc}", file=sys.stderr)
            return 2
        print(f"noted for {day.isoformat()}")
        return 0
    if args.coach_cmd == "notes":
        from .coach.store import recent_notes

        rows = recent_notes(conn, args.last)
        if not rows:
            print("no journal entries yet (wristpull coach note \"…\")")
            return 1
        for row in rows:
            print(f"{row['local_date']}  {row['note']}")
        return 0
    if args.coach_cmd == "plan-ics":
        from datetime import date, timedelta

        from .coach.plan import parse_plan_table, plan_to_ics
        from .coach.profile import coach_dir
        from .coach.store import last_weekly_plan
        from .timeutil import zone

        planned = last_weekly_plan(conn, "9999-12-31")
        rows = parse_plan_table(planned[1]) if planned else []
        if not rows:
            print("no weekly plan stored yet (run wristpull coach weekly first)", file=sys.stderr)
            return 1
        week_start = date.fromisoformat(planned[0]["period_end"]) + timedelta(days=1)
        out = args.out or (coach_dir(config) / f"plan-{week_start.isoformat()}.ics")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(plan_to_ics(rows, week_start, zone(config.timezone), hour=args.hour), "utf-8")
        print(f"wrote {len(rows)} events for the week of {week_start.isoformat()} to {out}")
        return 0
    if args.coach_cmd == "rate":
        from .coach.store import add_feedback

        try:
            add_feedback(conn, args.run_id, args.rating, args.note)
        except ValueError as exc:
            print(f"coach: {exc}", file=sys.stderr)
            return 2
        print(f"rated run #{args.run_id}: {args.rating:+d}")
        return 0
    if args.coach_cmd == "profile":
        from .coach.profile import apply_wizard, cli_asker, profile_path, run_wizard

        lang = resolve_language(config, args.lang)
        if args.wizard:
            answers = run_wizard(config, lang, cli_asker(lang))
            path = apply_wizard(config, answers, lang)
            print(f"\nprofile written: {path}")
        text, _sha, placeholders = load_profile(config, lang)
        print(f"profile: {profile_path(config)} ({len(text.splitlines())} lines)")
        print("open placeholders: " + (", ".join(placeholders) if placeholders else "none"))
        return 0 if not placeholders else 2
    log = (lambda _m: None) if args.quiet else print
    try:
        from .aggregate import parse_date
        from datetime import date

        today = parse_date(args.date) if args.date else date.today()
        backend = make_backend(config, model=args.model, backend=args.backend, kind=args.coach_cmd)
        result = run_coach(conn, config, args.coach_cmd, today=today, language=args.lang, question=getattr(args, "question", None), backend=backend, dry_run=args.dry_run, force=args.force, log=log)
    except (CoachError, ValueError) as exc:
        print(f"coach: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"  note: {warning}", file=sys.stderr)
    if result.status == "dry_run":
        print(f"dry run #{result.run_id}: prompt in {result.dry_run_path}")
        return 0
    print(result.answer_md.rstrip())
    cost = f", est. ${result.cost_usd:.4f}" if result.cost_usd else ""
    print(f"\n[run #{result.run_id}, {result.backend}/{result.model}, {result.duration_s:.0f} s, {result.input_tokens or '?'} in / {result.output_tokens or '?'} out tokens{cost}]", file=sys.stderr)
    return 0


def cmd_workout(args: argparse.Namespace, config: Config) -> int:
    from datetime import date, timedelta

    from .aggregate import parse_date
    from .workouts.model import describe_steps, template
    from .workouts.store import list_workouts, load, push_status, save, workouts_dir

    conn = _open(config)
    log = (lambda _m: None) if args.quiet else print
    try:
        if args.workout_cmd == "new":
            day = parse_date(args.date) if args.date else date.today() + timedelta(days=1)
            workout = template(args.template, day, config.workout.defaults(), name=args.name, main_minutes=args.minutes, main_metres=args.metres)
            path = save(config, workout)
            print(path)
            if args.edit:
                open_in_editor(path)
            return 0
        if args.workout_cmd == "list":
            start = parse_date(args.date_from) if args.date_from else None
            end = parse_date(args.date_to) if args.date_to else None
            items = list_workouts(config, start=start, end=end)
            if not items:
                print(f"no workout files in {workouts_dir(config)} (wristpull workout new)")
                return 1
            for w in items:
                total = f"{w.total_seconds // 60} min" + (f" + {w.total_metres} m" if w.total_metres else "")
                state = push_status(conn, w.path or "") or w.status
                flag = "" if w.valid else "  ✗ " + "; ".join(w.errors)
                print(f"{w.day.isoformat() if w.day else '----------'}  {w.name[:30]:<30}  {len(w.steps)} steps  {total:<16} {state}{flag}")
            return 0
        if args.workout_cmd == "check":
            from .workouts.store import mark_folder_clashes

            bad = 0
            checked = [load(file) for file in args.files]
            mark_folder_clashes(config, checked)
            for file, w in zip(args.files, checked):
                print(f"{file}: {w.name} ({w.day}) {'ok' if w.valid else 'invalid'}")
                for line in describe_steps(w):
                    print(f"  {line}")
                for error in w.errors:
                    print(f"  ✗ {error}", file=sys.stderr)
                bad += not w.valid
            return 0 if not bad else 2
        if args.workout_cmd == "from-plan":
            from .workouts.drafts import drafts_from_plan

            week = parse_date(args.week) if args.week else None
            result = drafts_from_plan(conn, config, week_start=week)
            if result.message:
                print(result.message, file=sys.stderr)
                return 1
            for path in result.created:
                print(f"  + {path}")
            for reason in result.skipped:
                print(f"  - {reason}")
            print(f"week of {result.week_start}: {len(result.created)} drafts written, {len(result.skipped)} days skipped")
            return 0
        if args.workout_cmd in ("watch", "remove"):
            from .workouts.watch import WORKOUT_LIMIT, read_watch, remove_from_watch

            listing = read_watch(conn, config, path=args.path)
            if listing.root is None:
                print("No Garmin device found. Connect the watch (MTP) or pass --path <GARMIN folder>.", file=sys.stderr)
                if listing.mount_message:
                    print(listing.mount_message, file=sys.stderr)
                return 1
            if args.workout_cmd == "remove":
                keys = list(args.names)
                if not args.yes:
                    wanted = {k.lower() for k in keys}
                    hits = [i for i in listing.items if {i.key.lower(), i.path.name.lower(), i.path.stem.lower(), i.name.lower()} & wanted]
                    keys = [i.key for i in hits if input(f'Remove "{i.name}" ({i.location}: {i.path.name}) from the watch? [y/N] ').strip().lower() in ("y", "yes", "j", "ja")]
                    unknown = wanted - {n for i in hits for n in (i.key.lower(), i.path.name.lower(), i.path.stem.lower(), i.name.lower())}
                    keys += sorted(unknown)  # reported as not found below
                result = remove_from_watch(conn, config, keys, path=listing.root, log=log)
                print(f"removed {len(result.removed)} workouts from the watch; backups in {result.backups[0].parent if result.backups else 'n/a'}")
                for key, error in result.errors:
                    print(f"  ✗ {key}: {error}", file=sys.stderr)
                if result.listing is not None:
                    print(f"{len(result.listing.items)} of ~{WORKOUT_LIMIT} workouts left on the watch")
                return 0 if not result.errors else 2
            for item in listing.items:
                created = item.time_created_utc[:16].replace("T", " ") if item.time_created_utc else "–"
                flags = (f"  ✗ {item.error}" if item.error else "") + ("  ⚠ same name as another workout" if item.name_clash else "")
                print(f"{item.location:<8} {item.name[:32]:<32}  {item.sport:<9} {item.steps:>2} steps  {created:<16}  {item.origin:<9}  {item.path.name}" + flags)
            print(f"{len(listing.active)} active + {len(listing.pending)} pending (active after unplugging) of ~{WORKOUT_LIMIT} workouts ({listing.root})")
            if any(i.name_clash for i in listing.items):
                print("warning: workouts with the same name may replace each other on the watch; give them distinct names.", file=sys.stderr)
            if listing.near_limit:
                print("warning: the fenix 6 stores 20-25 workouts; above that it drops new files silently. Remove old ones first (wristpull workout remove NAME).", file=sys.stderr)
            return 0
        if args.workout_cmd == "verify":
            from .workouts.push import verify

            result = verify(conn, config, path=args.path, log=log)
            if result.root is None:
                print("No Garmin device found. Connect the watch (MTP) or pass --path <GARMIN folder>.", file=sys.stderr)
                if result.mount_message:
                    print(result.mount_message, file=sys.stderr)
                return 1
            print(f"{result.root}: {len(result.on_watch)} workouts on the watch, {len(result.verified)} newly confirmed, {len(result.missing)} pushed but not found")
            if result.limit_warning:
                print(f"warning: {result.limit_warning}", file=sys.stderr)
            return 0 if not result.missing else 2
        # push
        from .workouts.push import push

        if args.files:
            selected = [load(f) for f in args.files]
        elif args.date:
            day = parse_date(args.date)
            selected = list_workouts(config, start=day, end=day)
        elif args.upcoming:
            from .workouts.store import upcoming

            selected = upcoming(config, conn, date.today(), force=args.force)
        else:
            print("pass workout files, --date or --upcoming", file=sys.stderr)
            return 2
        if not selected:
            print("nothing to push", file=sys.stderr)
            return 2
        result = push(conn, config, selected, path=args.path, folder=args.folder, schedule=True if args.schedule else None, dry_run=args.dry_run, eject=args.eject, force=args.force, log=log)
        if result.root is None and not result.invalid:
            print("No Garmin device found. Connect the watch (MTP) or pass --path <GARMIN folder>.", file=sys.stderr)
            if result.mount_message:
                print(result.mount_message, file=sys.stderr)
            return 1
        mode = "dry run: " if args.dry_run else ""
        print(f"{mode}{len(result.written)} workout files written to {result.target}" + (f", schedule {result.schedule.name}" if result.schedule else ""))
        if not args.dry_run and result.written:
            state = "The watch was ejected; unplug it now." if result.ejected else "Unplug the watch when you are done."
            print(state + (" It imports the files from NewFiles and lists them under Training > Workouts." if (args.folder or config.workout.folder) == "NewFiles" else " The files are in GARMIN/Workouts; a restart of the watch may be needed."))
        for name, errors in result.invalid:
            print(f"  ✗ {name}: " + "; ".join(errors), file=sys.stderr)
        return 0 if not result.invalid else 2
    except ValueError as exc:
        print(f"workout: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"workout: file exists: {exc}", file=sys.stderr)
        return 2


def open_in_editor(path: Path) -> int:
    """Open a file in $VISUAL / $EDITOR (nano as the fallback) and wait."""
    import os
    import shlex
    import subprocess

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    return subprocess.call([*shlex.split(editor), str(path)])


def _load_tui():
    """Import the TUI lazily so the core CLI works without the optional 'tui' extra."""
    from .tui.app import run_tui

    return run_tui


def cmd_tui(args: argparse.Namespace, config: Config) -> int:
    try:
        run_tui = _load_tui()
    except ImportError as exc:
        print(f"the terminal UI needs the optional dependencies: uv sync --extra tui ({exc})", file=sys.stderr)
        return 1
    return run_tui(config)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "import":
        if args.device == "gadgetbridge":
            return cmd_import_gadgetbridge(args, config)
        return cmd_import_garmin(args, config)
    if args.command == "pull":
        return cmd_pull(args, config)
    if args.command == "status":
        return cmd_status(args, config)
    if args.command == "inspect":
        return cmd_inspect(args, config)
    if args.command == "config":
        return cmd_config(args, config)
    if args.command == "mount":
        return cmd_mount(args, config)
    if args.command == "export":
        return cmd_export(args, config)
    if args.command == "tui":
        return cmd_tui(args, config)
    if args.command == "reparse":
        return cmd_reparse(args, config)
    if args.command == "coach":
        return cmd_coach(args, config)
    if args.command == "workout":
        return cmd_workout(args, config)
    return cmd_report(args, config)
