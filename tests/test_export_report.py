from datetime import date
from pathlib import Path

import polars as pl

from wristpull.aggregate import daily_frame, month_bounds, parse_date, week_bounds, weekly_frame
from wristpull.charts import bar_chart, line_chart
from wristpull.cli import main
from wristpull.export import export_for_claude
from wristpull.importers.garmin import import_garmin
from wristpull.report import write_report


def test_bounds():
    assert week_bounds(date(2026, 10, 31)) == (date(2026, 10, 26), date(2026, 11, 1))
    assert month_bounds(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds(date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert parse_date("2026-10-06") == date(2026, 10, 6)


def test_charts_are_valid_svg():
    svg = bar_chart("Steps", ["Mon", "Tue"], [1000, None], unit="")
    assert svg.startswith("<svg") and svg.endswith("</svg>") and svg.count("<rect") == 2  # background + one bar
    svg = line_chart("RHR", ["a", "b", "c"], {"Garmin": [50, None, 52], "Strap": [None, None, None]}, unit=" bpm")
    assert "<path" in svg and svg.count("<circle") == 2 and "Strap" in svg
    assert "<svg" in line_chart("empty", [], {"x": []})


def test_frames_from_imported_data(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    daily = daily_frame(conn, "2026-10-01", "2026-10-31")
    assert daily.height == 2 and daily["date"].dtype == pl.Date
    row = daily.filter(pl.col("date") == date(2026, 10, 6)).row(0, named=True)
    assert row["garmin_steps"] == 52 * 120 + 2000 and row["garmin_sleep_score"] == 78 and row["strap_steps"] is None
    weekly = weekly_frame(daily)
    assert weekly.height == 1
    week = weekly.row(0, named=True)
    assert week["week"] == "2026-W41" and week["days"] == 2 and week["activities"] == 1 and isinstance(week["activities"], int)
    assert week["training_load"] == 62  # 61.5 rounded
    assert weekly_frame(pl.DataFrame()).is_empty()


def test_export_writes_markdown_and_csv(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    result = export_for_claude(conn, config, "2026-10-01", "2026-10-31")
    names = sorted(p.name for p in result.files)
    assert names == ["activities.csv", "daily.csv", "sleep.csv", "summary.md", "weekly.csv"]
    assert result.directory == config.data_dir / "exports" / "2026-10-01_2026-10-31"
    md = result.markdown
    assert md.startswith("# Health data 2026-10-01 to 2026-10-31")
    assert "| Steps per day | 4120 | - |" in md  # mean over both days with data (0 + 8240)
    assert "| 2026-W41 |" in md
    assert "running" in md and "| 2026-10-06 | garmin |" in md  # activity + sleep rows
    assert "garmin_usb (2 days)" in md
    assert "## Attachments" in md and "`daily.csv`" in md
    assert sum(p.stat().st_size for p in result.files) < 20_000
    daily_csv = pl.read_csv(result.directory / "daily.csv")
    assert daily_csv.height == 2 and "garmin_steps" in daily_csv.columns

    empty = export_for_claude(conn, config, "2025-01-01", "2025-01-31", write_csv=False)
    assert "No data in this period." in empty.markdown and [p.name for p in empty.files] == ["summary.md"]


def test_report_week_and_month(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    week = write_report(conn, config, "week", date(2026, 10, 7), charts=True)
    assert (week.start, week.end) == (date(2026, 10, 5), date(2026, 10, 11))
    assert week.directory.name == "week-2026-W41"
    assert sorted(p.name for p in week.files) == ["report.md", "resting_hr.svg", "sleep.svg", "steps.svg", "stress.svg"]
    assert "# Week 2026-W41" in week.markdown and "![steps](steps.svg)" in week.markdown
    assert "- Steps: 4120 per day." in week.markdown  # no previous period -> no delta
    assert "## Nights" in week.markdown and "## Activities" in week.markdown
    month = write_report(conn, config, "month", date(2026, 10, 20))
    assert month.directory.name == "month-2026-10" and "# October 2026" in month.markdown
    assert [p.name for p in month.files] == ["report.md"]
    nothing = write_report(conn, config, "week", date(2025, 1, 1))
    assert "No data for this period." in nothing.markdown


def test_export_and_report_cli(garmin_tree: Path, tmp_path: Path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    assert main(["--config", str(cfg), "import", "garmin", "--path", str(garmin_tree)]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg), "export", "--for-claude", "--from", "2026-10-01", "--to", "2026-10-31", "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "# Health data 2026-10-01 to 2026-10-31" in out and "wrote 5 files" in out
    assert main(["--config", str(cfg), "export", "--from", "2026-10-31", "--to", "2026-10-01"]) == 2
    assert main(["--config", str(cfg), "export", "--from", "nonsense", "--to", "2026-10-01"]) == 2
    capsys.readouterr()
    assert main(["--config", str(cfg), "report", "week", "--date", "2026-10-06", "--charts"]) == 0
    assert "week 2026-10-05 to 2026-10-11: wrote 5 files" in capsys.readouterr().out
    assert main(["--config", str(cfg), "report", "month", "--date", "2026-10-06", "--stdout"]) == 0
    assert "# October 2026" in capsys.readouterr().out
