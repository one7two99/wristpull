"""Backup and restore of the whole state (data directory, config, .env)."""

from __future__ import annotations

import os
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wristpull import backup
from wristpull.db import SCHEMA_VERSION, connect

ENV_TEXT = """# comment stays
UID=4242
GID=4242
WRISTPULL_DATA=/home/old/Health/wristpull
WRISTPULL_CONFIG=/home/old/.config/wristpull/config.toml
WRISTPULL_WEB_SECRET=keep-me
export OPENROUTER_API_KEY="sk-quoted"
WRISTPULL_STRAP_AUTHKEY=0x00112233445566778899aabbccddeeff
"""


def make_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "old" / "Health" / "wristpull"
    raw = data / "raw" / "garmin_usb" / "2026"
    raw.mkdir(parents=True)
    (raw / "A.FIT").write_bytes(b"\x0e" * 40)
    (raw / "A.FIT").chmod(0o444)
    (data / "adb").mkdir()
    (data / "adb" / "adbkey").write_text("private", "utf-8")
    (data / "adb" / "adbkey").chmod(0o600)
    (data / "staging").mkdir()
    (data / "staging" / "half.FIT").write_bytes(b"partial")
    (data / "workouts").mkdir()  # empty folders survive
    conn = connect(data / "wristpull.sqlite")
    conn.execute("INSERT INTO journal (local_date, note, created_at_utc) VALUES ('2026-11-07', 'kept', '2026-11-08T07:00:00Z')")
    conn.commit()  # WAL mode: the -wal/-shm files exist while the connection is open
    config = tmp_path / "old" / ".config" / "wristpull" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[storage]\ndata_dir = "/home/old/Health/wristpull"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    env = tmp_path / "old" / "dev" / "wristpull" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text(ENV_TEXT, "utf-8")
    conn.execute("INSERT INTO journal (local_date, note, created_at_utc) VALUES ('2026-11-08', 'still open', '2026-11-08T07:01:00Z')")
    conn.commit()
    return data, config, env  # conn stays open on purpose: the dashboard may be running


def test_create_and_restore_round_trip(tmp_path: Path):
    data, config, env = make_state(tmp_path)
    out = tmp_path / "backups"
    logged: list[str] = []
    now = datetime(2026, 11, 8, 9, 5, tzinfo=UTC)
    stats = backup.create_backup(data, config, env, out, log=logged.append, now=now)
    assert stats.path == out / backup.default_name(now) and stats.path.name.startswith("wristpull-backup-2026-11-08-")
    assert oct(stats.path.stat().st_mode & 0o777) == "0o600" and not list(out.glob("*.part"))
    m = stats.manifest
    assert m.schema_version == SCHEMA_VERSION and m.has_env and m.has_config
    assert m.data_dir == "/home/old/Health/wristpull" and m.config_file == "/home/old/.config/wristpull/config.toml"
    assert m.files == 5  # A.FIT, adbkey, wristpull.sqlite, env, config
    with tarfile.open(stats.path) as tar:
        names = tar.getnames()
    assert names[:2] == ["wristpull-backup", "wristpull-backup/MANIFEST.json"] and names[-1] == "wristpull-backup/SHA256SUMS"
    assert "wristpull-backup/data/workouts" in names and "wristpull-backup/data/wristpull.sqlite" in names
    assert not any("staging" in n or "-wal" in n or "-shm" in n for n in names)
    assert backup.read_manifest(stats.path) == m
    assert any("database copied" in line for line in logged)

    new_data = tmp_path / "new" / "Health" / "wristpull"
    new_config = tmp_path / "new" / ".config" / "wristpull" / "config.toml"
    new_env = tmp_path / "new" / "dev" / "wristpull" / ".env"
    new_config.parent.mkdir(parents=True)
    new_config.touch()  # the empty bind-mount target counts as absent
    restored = backup.restore_backup(stats.path, data_dir=new_data, config_path=new_config, env_path=new_env, uid=1000, gid=1000)
    assert restored.files == 5 and restored.notes == []
    assert (new_data / "raw" / "garmin_usb" / "2026" / "A.FIT").read_bytes() == b"\x0e" * 40
    assert oct((new_data / "raw" / "garmin_usb" / "2026" / "A.FIT").stat().st_mode & 0o777) == "0o444"
    assert oct((new_data / "adb" / "adbkey").stat().st_mode & 0o777) == "0o600"
    assert (new_data / "workouts").is_dir() and not (new_data / "staging").exists()
    rows = sqlite3.connect(new_data / "wristpull.sqlite").execute("SELECT note FROM journal ORDER BY local_date").fetchall()
    assert [r[0] for r in rows] == ["kept", "still open"]
    env_text = new_env.read_text("utf-8")
    assert "# comment stays" in env_text and "WRISTPULL_WEB_SECRET=keep-me" in env_text
    assert "UID=1000\nGID=1000\n" in env_text and f"WRISTPULL_DATA={new_data}\n" in env_text and f"WRISTPULL_CONFIG={new_config}\n" in env_text
    assert 'export OPENROUTER_API_KEY="sk-quoted"' in env_text and "WRISTPULL_STRAP_AUTHKEY=0x0011" in env_text
    assert oct(new_env.stat().st_mode & 0o777) == "0o600"
    cfg_text = new_config.read_text("utf-8")
    assert f'data_dir = "{new_data}"' in cfg_text and 'timezone = "Europe/Berlin"' in cfg_text

    # occupied targets are refused without force; with force the archive's files win, nothing else goes
    (new_data / "extra.txt").write_text("mine", "utf-8")
    with pytest.raises(backup.BackupError, match="not empty.*--force"):
        backup.restore_backup(stats.path, data_dir=new_data, config_path=new_config, env_path=new_env)
    again = backup.restore_backup(stats.path, data_dir=new_data, config_path=new_config, env_path=new_env, force=True, host_data_dir=Path("/srv/wp"), host_config=Path("/srv/config.toml"))
    assert again.files == 5 and (new_data / "extra.txt").read_text("utf-8") == "mine"
    assert "WRISTPULL_DATA=/srv/wp\n" in new_env.read_text("utf-8") and 'data_dir = "/srv/wp"' in new_config.read_text("utf-8")


def test_create_without_env_and_config(tmp_path: Path):
    data, _config, _env = make_state(tmp_path)
    stats = backup.create_backup(data, None, tmp_path / "missing.env", tmp_path / "b.tar.gz")
    assert stats.path == tmp_path / "b.tar.gz" and not stats.manifest.has_env and not stats.manifest.has_config
    assert stats.manifest.data_dir == str(data)
    target = tmp_path / "restored"
    restored = backup.restore_backup(stats.path, data_dir=target, config_path=tmp_path / "c.toml", env_path=tmp_path / "e")
    assert restored.config_path is None and restored.env_path is None and (target / "adb" / "adbkey").exists()
    assert not (tmp_path / "c.toml").exists() and not (tmp_path / "e").exists()
    with pytest.raises(backup.BackupError, match="refusing to overwrite"):
        backup.create_backup(data, None, None, tmp_path / "b.tar.gz")
    with pytest.raises(backup.BackupError, match="data directory not found"):
        backup.create_backup(tmp_path / "nowhere", None, None, tmp_path)


def test_bad_archives(tmp_path: Path):
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tar")
    with pytest.raises(backup.BackupError, match="cannot read"):
        backup.read_manifest(tmp_path / "junk.tar.gz")
    with tarfile.open(tmp_path / "other.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("something/else")
        tar.addfile(info)
    with pytest.raises(backup.BackupError, match="unexpected entry"):
        backup.read_manifest(tmp_path / "other.tar.gz")
    # a manifest from a newer format, an escaping member, a wrong checksum
    with pytest.raises(backup.BackupError, match="unsupported backup format"):
        backup.Manifest.from_json('{"format": 99}')
    data, _c, _e = make_state(tmp_path)
    stats = backup.create_backup(data, None, None, tmp_path / "ok.tar.gz")
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(stats.path) as src, tarfile.open(evil, "w:gz") as dst:
        for member in src:
            fh = src.extractfile(member) if member.isfile() else None
            if member.name.endswith("adb/adbkey"):
                member.name = "wristpull-backup/data/../../escaped"
            dst.addfile(member, fh)
    with pytest.raises(backup.BackupError, match="unsafe entry"):
        backup.restore_backup(evil, data_dir=tmp_path / "r1", config_path=None, env_path=None)
    assert not (tmp_path / "escaped").exists()
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(stats.path) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src:
            fh = src.extractfile(member) if member.isfile() else None
            if member.name.endswith("adb/adbkey"):
                import io

                fh = io.BytesIO(b"changed")
                member.size = 7
            dst.addfile(member, fh)
    with pytest.raises(backup.BackupError, match="checksum mismatch: data/adb/adbkey"):
        backup.restore_backup(tampered, data_dir=tmp_path / "r2", config_path=None, env_path=None)


def test_env_and_config_rewriting():
    values = backup.parse_env(ENV_TEXT)
    assert values["UID"] == "4242" and values["OPENROUTER_API_KEY"] == "sk-quoted" and "# comment stays" not in values
    text = backup.rewrite_env("A=1\n# B=2\nB=old\n", {"B": "new", "C": "3"})
    assert text == "A=1\n# B=2\nB=new\n# set by wristpull backup restore\nC=3\n"
    assert backup.rewrite_data_dir('[time]\ntimezone = "UTC"\n', Path("/d")) == '[storage]\ndata_dir = "/d"\n\n[time]\ntimezone = "UTC"\n'
    assert backup.rewrite_data_dir('[storage]\n# note\ndata_dir = "~/x"\n[time]\n', Path("/d")) == '[storage]\n# note\ndata_dir = "/d"\n[time]\n'
    assert backup.rewrite_data_dir("[storage]\nother = 1\n", Path("/d")) == '[storage]\nother = 1\ndata_dir = "/d"\n'
    assert backup.default_name(datetime(2026, 1, 2, 3, 4, tzinfo=UTC)) == f"wristpull-backup-{datetime(2026, 1, 2, 3, 4, tzinfo=UTC).astimezone():%Y-%m-%d-%H%M}.tar.gz"
    assert list(backup.describe(backup.Manifest(has_env=True)))[3] == "env:       included (secrets!)"


def test_restore_uses_the_current_user_by_default(tmp_path: Path):
    data, config, env = make_state(tmp_path)
    stats = backup.create_backup(data, config, env, tmp_path)
    new_env = tmp_path / "n" / ".env"
    backup.restore_backup(stats.path, data_dir=tmp_path / "n" / "data", config_path=tmp_path / "n" / "c.toml", env_path=new_env)
    assert f"UID={os.getuid()}\nGID={os.getgid()}\n" in new_env.read_text("utf-8")
