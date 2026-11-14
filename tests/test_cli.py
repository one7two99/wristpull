from pathlib import Path

import pytest

from wristpull.cli import main


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path


def test_import_status_and_inspect(cfg_file: Path, garmin_tree: Path, capsys):
    assert main(["--config", str(cfg_file), "import", "garmin", "--path", str(garmin_tree)]) == 0
    out = capsys.readouterr().out
    assert "5 FIT files, 5 new, 0 already archived, 0 errors" in out
    assert "activities=1" in out

    assert main(["--config", str(cfg_file), "status"]) == 0
    out = capsys.readouterr().out
    assert "garmin_usb" in out and "monitor" in out
    assert "heart_rate" in out and "672" in out
    assert "daily_summary" in out

    fit_file = garmin_tree / "Monitor" / "M6A10500.FIT"
    assert main(["--config", str(cfg_file), "inspect", str(fit_file), "--messages", "stress_level", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "stress_level (#227): 96" in out
    assert out.count("[stress_level]") == 2


def test_import_without_device(cfg_file: Path, tmp_path: Path, capsys):
    assert main(["--config", str(cfg_file), "import", "garmin", "--path", str(tmp_path / "empty")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err


def test_config_init_and_show(tmp_path: Path, capsys):
    cfg = tmp_path / "c" / "config.toml"
    assert main(["--config", str(cfg), "config", "--init"]) == 0
    assert cfg.exists()
    assert main(["--config", str(cfg), "config"]) == 0
    assert "[storage]" in capsys.readouterr().out


def test_export_requires_dates(cfg_file: Path):
    with pytest.raises(SystemExit):
        main(["--config", str(cfg_file), "export"])


def test_import_gadgetbridge_cli(cfg_file: Path, tmp_path: Path, capsys):
    from gb_db import build_gb_db

    db = build_gb_db(tmp_path / "Gadgetbridge.db")
    assert main(["--config", str(cfg_file), "import", "gadgetbridge", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Amazfit Helio Strap" in out and "heart_rate=295" in out
    assert main(["--config", str(cfg_file), "import", "gadgetbridge"]) == 2


def test_pull_cli_without_adb_device(cfg_file: Path, capsys, monkeypatch):
    from wristpull import cli
    from wristpull.adb import NoDeviceError

    def boom(*args, **kwargs):
        raise NoDeviceError("no authorized device connected")

    monkeypatch.setattr(cli, "pull_phone", boom)
    assert main(["--config", str(cfg_file), "pull", "phone"]) == 1
    assert "no authorized device" in capsys.readouterr().err


def test_tui_without_textual_explains(cfg_file: Path, capsys, monkeypatch):
    from wristpull import cli

    def missing():
        raise ImportError("No module named 'textual'")

    monkeypatch.setattr(cli, "_load_tui", missing)
    assert main(["--config", str(cfg_file), "tui"]) == 1
    assert "uv sync --extra tui" in capsys.readouterr().err


def test_tui_command_runs_app(cfg_file: Path, monkeypatch):
    from wristpull import cli

    seen = {}

    def fake_run(config):
        seen["data_dir"] = config.data_dir
        return 0

    monkeypatch.setattr(cli, "_load_tui", lambda: fake_run)
    assert main(["--config", str(cfg_file), "tui"]) == 0
    assert seen["data_dir"].name == "Health"


def test_workout_commands(cfg_file: Path, tmp_path: Path, garmin_tree: Path, capsys):
    base = ["--config", str(cfg_file), "workout"]
    assert main([*base, "list"]) == 1
    assert "no workout files" in capsys.readouterr().out
    assert main([*base, "new", "--date", "2026-11-05", "--name", "Easy run"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.name == "2026-11-05-easy-run.md" and path.exists()
    assert main([*base, "new", "--date", "2026-11-06", "--template", "distance", "--metres", "8000"]) == 0
    capsys.readouterr()
    assert main([*base, "new", "--date", "2026-11-06", "--template", "distance", "--metres", "8000"]) == 2  # exists
    assert "file exists" in capsys.readouterr().err
    assert main([*base, "check", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Easy run (2026-11-05) ok" in out and "1 warmup 10:00  HR <= 130" in out and "2 active 40:00  HR Z2" in out
    bad = path.parent / "bad.md"
    bad.write_text("---\ndate: 2026-11-07\nname: Bad\n---\n- sprint 5 min\n", "utf-8")
    assert main([*base, "check", str(bad)]) == 2
    assert "unknown intensity" in capsys.readouterr().err
    assert main([*base, "list"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-05  Easy run" in out and "55 min" in out and "draft" in out and "✗ unknown intensity" in out and "8000 m" in out
    assert main([*base, "push"]) == 2
    assert main([*base, "push", "--date", "2026-11-05", "--dry-run", "--schedule"]) == 0
    out = capsys.readouterr().out
    assert "dry run: 1 workout files written to" in out and "schedule" in out
    assert main([*base, "push", str(path), "--path", str(tmp_path / "nowhere")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err
    assert main([*base, "push", str(path), str(bad), "--path", str(garmin_tree)]) == 2  # the bad one is reported
    captured = capsys.readouterr()
    assert "1 workout files written to" in captured.out and "Unplug the watch" in captured.out and "Bad:" in captured.err
    assert len(list((garmin_tree / "NewFiles").glob("*.fit"))) == 1
    assert main([*base, "list"]) == 0
    assert "pushed" in capsys.readouterr().out
    assert main([*base, "verify", "--path", str(garmin_tree)]) == 2  # pushed but not under Workouts yet
    assert "1 pushed but not found" in capsys.readouterr().out
    (garmin_tree / "Workouts").mkdir()
    (garmin_tree / "Workouts" / "W.FIT").write_bytes(next((garmin_tree / "NewFiles").glob("*.fit")).read_bytes())
    assert main([*base, "verify", "--path", str(garmin_tree)]) == 0
    assert "1 newly confirmed" in capsys.readouterr().out
    assert main([*base, "verify", "--path", str(tmp_path / "nowhere")]) == 1
    capsys.readouterr()
    assert main([*base, "from-plan"]) == 1
    assert "no weekly plan" in capsys.readouterr().err
    assert main([*base, "push", "--upcoming", "--dry-run"]) in (0, 2)
    # a second "Easy run" in the same week: check and list flag it, push refuses it
    clash = path.parent / "2026-11-07-easy-run.md"
    clash.write_text("---\ndate: 2026-11-07\nname: Easy run\n---\n- active 30 min\n", "utf-8")
    assert main([*base, "check", str(clash)]) == 2
    assert "name also used by 2026-11-05-easy-run.md in the same week" in capsys.readouterr().err
    assert main([*base, "push", str(clash), "--dry-run"]) == 2
    capsys.readouterr()


def test_workout_watch_remove_and_force(cfg_file: Path, tmp_path: Path, garmin_tree: Path, capsys, monkeypatch):
    base = ["--config", str(cfg_file), "workout"]
    assert main([*base, "watch", "--path", str(tmp_path / "nowhere")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err
    assert main([*base, "watch", "--path", str(garmin_tree)]) == 0
    assert "0 active + 0 pending (active after unplugging) of ~25 workouts" in capsys.readouterr().out
    assert main([*base, "new", "--date", "2099-01-05", "--name", "Future run"]) == 0
    capsys.readouterr()
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree), "--folder", "Workouts"]) == 0
    capsys.readouterr()
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree)]) == 2  # nothing left without --force
    assert "nothing to push" in capsys.readouterr().err
    assert main([*base, "push", "--upcoming", "--force", "--path", str(garmin_tree), "--folder", "Workouts"]) == 0
    capsys.readouterr()
    assert main([*base, "watch", "--path", str(garmin_tree)]) == 0
    out = capsys.readouterr().out
    assert out.count("Future run") == 2 and "wristpull" in out and "2 active + 0 pending" in out
    # interactive removal: answer no, then yes
    answers = iter(["n", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert main([*base, "remove", "Future run", "--path", str(garmin_tree)]) == 0
    assert "removed 0 workouts" in capsys.readouterr().out
    assert main([*base, "remove", "Future run", "missing", "--path", str(garmin_tree), "--yes"]) == 2
    captured = capsys.readouterr()
    assert "removed 2 workouts" in captured.out and "0 of ~25 workouts left" in captured.out and "missing: not found" in captured.err
    assert len(list((tmp_path / "Health" / "workouts" / "watch-backup").iterdir())) >= 1
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree)]) == 0  # removed -> upcoming again


def test_open_in_editor_without_editor_explains(tmp_path: Path, capsys, monkeypatch):
    from wristpull.cli import open_in_editor

    monkeypatch.setenv("VISUAL", "")
    monkeypatch.setenv("EDITOR", str(tmp_path / "no-such-editor"))
    target = tmp_path / "w.md"
    target.write_text("x")
    assert open_in_editor(target) == 2
    assert "editor not found" in capsys.readouterr().err


def test_import_and_status_over_mtp(cfg_file: Path, garmin_tree: Path, capsys, monkeypatch):
    """Without --path the CLI drives the real MtpTools parsers against a fake runner."""
    import wristpull.mtp as mtp_module
    from fake_mtp import FakeMtp, fake_mtp_runner

    fake = FakeMtp(garmin_tree.parent)
    monkeypatch.setattr(mtp_module, "run_command", fake_mtp_runner(fake))
    assert main(["--config", str(cfg_file), "import", "garmin", "--dry-run"]) == 0
    assert "mtp://GARMIN: 5 FIT files, 5 new" in capsys.readouterr().out and fake.fetched == []
    assert main(["--config", str(cfg_file), "import", "garmin"]) == 0
    assert "mtp://GARMIN: 5 FIT files, 5 new, 0 already archived" in capsys.readouterr().out and len(fake.fetched) == 5
    assert main(["--config", str(cfg_file), "status"]) == 0
    assert "connected over USB (MTP): fenix 6 Pro" in capsys.readouterr().out
    fake.present = False
    assert main(["--config", str(cfg_file), "import", "garmin"]) == 1
    assert "no MTP device" in capsys.readouterr().err


def test_workout_remove_cli_over_mtp_reuses_the_connection(cfg_file: Path, garmin_tree: Path, capsys, monkeypatch):
    import wristpull.mtp as mtp_module
    from fake_mtp import FakeMtp, fake_mtp_runner
    from wristpull.config import load_config
    from wristpull.workouts.model import Step, Workout
    from wristpull.workouts.store import save

    (garmin_tree / "Workouts").mkdir()
    (garmin_tree / "Workouts" / "W1.FIT").write_bytes(__import__("wristpull.fit.workout", fromlist=["build_workout_fit"]).build_workout_fit(Workout(name="Old one", steps=[Step("active", duration_s=600)])))
    save(load_config(cfg_file), Workout(name="Easy run", steps=[Step("active", duration_s=600)]))
    fake = FakeMtp(garmin_tree.parent)
    monkeypatch.setattr(mtp_module, "run_command", fake_mtp_runner(fake))
    monkeypatch.setenv("WRISTPULL_IN_CONTAINER", "1")
    assert main(["--config", str(cfg_file), "workout", "watch"]) == 0
    out = capsys.readouterr().out
    assert "1 active + 0 pending" in out and "mtp://GARMIN on fenix 6 Pro" in out
    assert main(["--config", str(cfg_file), "workout", "remove", "--yes", "Old one"]) == 0
    out = capsys.readouterr().out
    assert "removed 1 workouts" in out and "0 of ~25 workouts left" in out and fake.deleted and not (garmin_tree / "Workouts" / "W1.FIT").exists()


def test_strap_commands(cfg_file: Path, tmp_path: Path, capsys, monkeypatch):
    from fake_band import FAKE_ADDRESS, FAKE_KEY, FakeBandState, FakeTransport
    from zepp_buffers import stress_minutes
    from wristpull.config import load_config
    from wristpull.zeppos import transport as transport_module

    monkeypatch.delenv("WRISTPULL_STRAP_AUTHKEY", raising=False)
    # info before anything is configured
    assert main(["--config", str(cfg_file), "strap", "info"]) == 0
    out = capsys.readouterr().out
    assert "(not configured)" in out and "missing (set WRISTPULL_STRAP_AUTHKEY" in out and "last pull: never" in out
    # pair prints the .env line and writes the address, no probe, no file
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", "0x" + FAKE_KEY.hex(), "--address", FAKE_ADDRESS.lower(), "--no-probe"]) == 0
    out = capsys.readouterr().out
    assert f"WRISTPULL_STRAP_AUTHKEY=0x{FAKE_KEY.hex()}" in out and "put this line into .env" in out
    config = load_config(cfg_file)
    assert config.strap_address == FAKE_ADDRESS
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", "nope", "--no-probe"]) == 1
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", FAKE_KEY.hex(), "--address", "not-an-address", "--no-probe"]) == 1
    # without the key the pull explains what to set
    assert main(["--config", str(cfg_file), "pull", "strap"]) == 1
    assert "WRISTPULL_STRAP_AUTHKEY" in capsys.readouterr().err
    # the key lives in the environment (.env for Docker, the shell for a native install)
    monkeypatch.setenv("WRISTPULL_STRAP_AUTHKEY", "0x" + FAKE_KEY.hex())
    assert main(["--config", str(cfg_file), "status"]) == 0
    assert f"Strap:      {FAKE_ADDRESS} over Bluetooth, key from the environment, last pull never" in capsys.readouterr().out
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", FAKE_KEY.hex(), "--no-probe"]) == 0
    assert "already set to this key" in capsys.readouterr().out
    # pull strap through a fake band (the default transport factory is replaced; conftest blocks the real one)
    state = FakeBandState()
    from datetime import UTC, datetime

    state.minutes["stress"] = (datetime(2026, 11, 6, 23, 0, tzinfo=UTC), stress_minutes([20, None, 35]))
    monkeypatch.setattr(transport_module, "open_transport", lambda address, adapter=None, log=None: FakeTransport(state))
    assert main(["--config", str(cfg_file), "pull", "strap", "--dry-run"]) == 0
    assert "dry run: strap" in capsys.readouterr().out
    assert main(["--config", str(cfg_file), "pull", "strap"]) == 0
    out = capsys.readouterr().out
    assert "stress=3" in out and "1 new buffers" in out and "inserted: stress=2" in out
    assert main(["--config", str(cfg_file), "strap", "info"]) == 0
    out = capsys.readouterr().out
    assert "stress       up to 2026-11-06T23:02:00Z" in out and "from the environment" in out
    # pair with a probe answers with the band's label
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", FAKE_KEY.hex()]) == 0
    assert "band answers: serial 24400000000001" in capsys.readouterr().out
    # wrong key: the band refuses, the CLI reports it
    assert main(["--config", str(cfg_file), "strap", "pair", "--authkey", "11" * 16]) == 1
    assert "rejected the auth key" in capsys.readouterr().err
    monkeypatch.setenv("WRISTPULL_STRAP_AUTHKEY", "11" * 16)
    assert main(["--config", str(cfg_file), "pull", "strap"]) == 1


def test_backup_commands(cfg_file: Path, tmp_path: Path, capsys, monkeypatch):
    data = tmp_path / "Health"
    assert main(["--config", str(cfg_file), "status"]) == 0  # creates the database
    (data / "coach").mkdir()
    (data / "coach" / "goals.md").write_text("# goals\n", "utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "compose.yaml").write_text("services: {}\n", "utf-8")
    (checkout / ".env").write_text("UID=1\nGID=1\nWRISTPULL_DATA=/old\nWRISTPULL_CONFIG=/old.toml\nWRISTPULL_WEB_SECRET=s\n", "utf-8")
    (checkout / "deploy" / "certs").mkdir(parents=True)
    (checkout / "deploy" / "certs" / "wristpull.pem").write_text("cert\n", "utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("WRISTPULL_ENV_FILE", raising=False)
    monkeypatch.delenv("WRISTPULL_CERTS_DIR", raising=False)
    assert main(["--config", str(cfg_file), "backup", "create"]) == 0  # default target: <data_dir>/backups/
    text = capsys.readouterr().out
    assert "backup written: " in text and "contains .env" in text
    archive = next((data / "backups").glob("wristpull-backup-*.tar.gz"))
    assert main(["backup", "show", str(archive)]) == 0
    text = capsys.readouterr().out
    assert "env:       included (secrets!)" in text and "data dir:  /old" in text and "certs:     wristpull.pem" in text
    out = tmp_path / "backups"
    assert main(["--config", str(cfg_file), "backup", "create", "--out", str(out)]) == 0
    capsys.readouterr()
    archive = next(out.glob("wristpull-backup-*.tar.gz"))
    # restore into a second checkout and data directory
    new_checkout = tmp_path / "new"
    new_checkout.mkdir()
    (new_checkout / "compose.yaml").write_text("services: {}\n", "utf-8")
    monkeypatch.chdir(new_checkout)
    new_cfg = tmp_path / "new.toml"
    assert main(["--config", str(new_cfg), "backup", "restore", str(archive), "--data-dir", str(tmp_path / "new-data"), "--uid", "7", "--gid", "8"]) == 0
    text = capsys.readouterr().out
    assert f"restored into {tmp_path / 'new-data'}" in text and "next: check WRISTPULL_SITE" in text
    assert (tmp_path / "new-data" / "coach" / "goals.md").read_text("utf-8") == "# goals\n"
    assert f'data_dir = "{tmp_path / "new-data"}"' in new_cfg.read_text("utf-8")
    env_text = (new_checkout / ".env").read_text("utf-8")
    assert "UID=7\nGID=8\n" in env_text and f"WRISTPULL_CONFIG={new_cfg}\n" in env_text and "WRISTPULL_WEB_SECRET=s" in env_text
    assert not (new_checkout / "deploy").exists()  # no certs folder in the new checkout: the files are skipped with a note
    (new_checkout / "deploy" / "certs").mkdir(parents=True)
    assert main(["--config", str(new_cfg), "backup", "restore", str(archive), "--data-dir", str(tmp_path / "new-data"), "--force"]) == 0
    assert (new_checkout / "deploy" / "certs" / "wristpull.pem").read_text("utf-8") == "cert\n"
    assert main(["--config", str(new_cfg), "status"]) == 0
    # a second restore refuses to touch the occupied targets
    assert main(["--config", str(new_cfg), "backup", "restore", str(archive), "--data-dir", str(tmp_path / "new-data")]) == 1
    assert "--force" in capsys.readouterr().err
    # without a checkout around, create says that .env is missing
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(new_cfg), "backup", "create", "--out", str(tmp_path / "second.tar.gz")]) == 0
    assert "no .env included" in capsys.readouterr().out
    assert main(["backup", "show", str(tmp_path / "nope.tar.gz")]) == 1
