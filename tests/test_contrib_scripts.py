"""The host-side shell scripts under contrib/bin: syntax, and the setup script end to end with a fake
docker and a fake sudo on the PATH (nothing real is touched, no root, no containers)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["wristpull", "wristpull-morning", "wristpull-backup", "wristpull-restore", "wristpull-setup"]
FAKE_KEY = "00112233445566778899AABBCCDDEEFF"


def test_scripts_parse():
    for name in SCRIPTS:
        subprocess.run(["bash", "-n", str(REPO / "contrib" / "bin" / name)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-S", "warning", *(str(REPO / "contrib" / "bin" / n) for n in SCRIPTS)], check=True)


def make_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A copy of what the setup script touches, plus a bin dir with fake docker/sudo/id/sleep/lsusb/bluetoothd."""
    checkout = tmp_path / "checkout"
    (checkout / "contrib" / "bin").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(REPO / "contrib" / "bin" / name, checkout / "contrib" / "bin" / name)
    for sub in ("udev", "apparmor"):
        shutil.copytree(REPO / "contrib" / sub, checkout / "contrib" / sub)
    shutil.copy(REPO / "compose.yaml", checkout / "compose.yaml")
    shutil.copy(REPO / ".env.example", checkout / ".env.example")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    stubs = {
        "docker": f"""#!/bin/sh
echo "docker $*" >> {log}
case "$1 $2" in
  "--version ") echo "Docker version 29.0.0, build fake" ;;
  "compose version") echo "v5.0.0" ;;
  "image inspect") exit 1 ;;
esac
case "$*" in *"serve --hash-password"*) echo "WRISTPULL_WEB_PASSWORD_HASH=pbkdf2\\$fake" ;; esac
exit 0
""",
        "sudo": f"""#!/bin/sh
echo "sudo $*" >> {log}
case "$1" in cp|install) exec "$@" ;; esac   # file installs really happen (into the temp dirs the test names)
exit 0
""",
        "id": """#!/bin/sh
case "$1" in -nG) echo "docker plugdev bluetooth" ;; *) exec /usr/bin/id "$@" ;; esac
""",
        "sleep": "#!/bin/sh\nexit 0\n",
        "lsusb": "#!/bin/sh\nexit 0\n",
        "bluetoothd": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        path = fake_bin / name
        path.write_text(body, "utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return checkout, fake_bin, log


def run_setup(checkout: Path, fake_bin: Path, home: Path, answers: list[str], *args: str) -> subprocess.CompletedProcess:
    system = home.parent / "system"
    (system / "udev").mkdir(parents=True, exist_ok=True)
    (system / "apparmor").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "HOME": str(home), "NO_COLOR": "1", "EDITOR": "true",
           "WRISTPULL_UDEV_DIR": str(system / "udev"), "WRISTPULL_APPARMOR_DIR": str(system / "apparmor")}
    env.pop("XDG_CONFIG_HOME", None)
    return subprocess.run(
        ["bash", str(checkout / "contrib" / "bin" / "wristpull-setup"), *args],
        input="\n".join(answers) + "\n", capture_output=True, text=True, env=env, timeout=60,
    )


def test_setup_dry_run_writes_nothing(tmp_path: Path):
    checkout, fake_bin, log = make_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    answers = [
        "n",            # restore from an archive?
        "", "",         # data directory, config file (defaults)
        "",             # WRISTPULL_SITE
        "n",            # set the dashboard password now?
        "", "",         # OpenRouter, Anthropic
        "0x" + FAKE_KEY,  # strap key
        "n",            # open the config in the editor?
    ]
    result = run_setup(checkout, fake_bin, home, answers, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "dry run" in out and "Step 11/11" in out and "Done" in out
    for expected in ("$ cp .env.example", "$ docker compose build", "wristpull config --init", "# cp contrib/udev/91-wristpull-garmin-docker.rules",
                     "# apparmor_parser -r -W ", "/apparmor/wristpull-cli", "wristpull status"):
        assert expected in out, expected
    assert "WRISTPULL_STRAP_AUTHKEY=…" in out and FAKE_KEY.lower() not in out and FAKE_KEY not in out
    assert not (checkout / ".env").exists() and not (home / "Health").exists()
    # only the read-only docker checks ran (version, compose version, image inspect): nothing was built or started
    assert all(line.startswith(("docker --version", "docker compose version", "docker image inspect")) for line in log.read_text("utf-8").splitlines())


def test_setup_full_run_with_fakes(tmp_path: Path):
    checkout, fake_bin, log = make_checkout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    config = tmp_path / "cfg" / "config.toml"
    answers = [
        "n",                      # restore?
        str(data), str(config),   # paths
        "",                       # WRISTPULL_SITE default
        "y",                      # set the dashboard password (fake docker prints a hash)
        "sk-or-test", "",         # OpenRouter key, no Anthropic key
        "0X" + FAKE_KEY,          # strap key, odd casing
        "n",                      # editor
        "y", "y",                 # sudo cp rule, sudo udevadm
        "y", "y",                 # sudo install profile, sudo apparmor_parser
        "n",                      # find the band now?
    ]
    result = run_setup(checkout, fake_bin, home, answers)
    assert result.returncode == 0, result.stdout + result.stderr
    env_file = checkout / ".env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    text = env_file.read_text("utf-8")
    assert f"UID={os.getuid()}\n" in text and f"WRISTPULL_DATA={data}\n" in text and f"WRISTPULL_CONFIG={config}\n" in text
    assert "WRISTPULL_WEB_PASSWORD_HASH=pbkdf2$fake\n" in text and "OPENROUTER_API_KEY=sk-or-test\n" in text
    assert f"WRISTPULL_STRAP_AUTHKEY=0x{FAKE_KEY.lower()}\n" in text and "# ANTHROPIC_API_KEY=" in text
    secret = [line for line in text.splitlines() if line.startswith("WRISTPULL_WEB_SECRET=")][0]
    assert len(secret) > len("WRISTPULL_WEB_SECRET=") + 30
    assert "# Copy to .env" in text  # comments of the example survive
    assert data.is_dir() and config.is_file() and config.stat().st_size == 0
    calls = log.read_text("utf-8")
    assert "docker compose build" in calls and "wristpull config --init" in calls and "docker compose up -d" in calls
    assert "sudo cp contrib/udev/91-wristpull-garmin-docker.rules " in calls and "sudo udevadm control --reload-rules" in calls
    assert "sudo apparmor_parser -r -W " in calls and (tmp_path / "system" / "apparmor" / "wristpull-cli").read_bytes() == (REPO / "contrib" / "apparmor" / "wristpull-cli").read_bytes()
    assert (tmp_path / "system" / "udev" / "91-wristpull-garmin-docker.rules").is_file()
    assert "wristpull status" in calls and FAKE_KEY not in result.stdout
    # a second run asks nothing that is already answered: every value reports ok (paths, the still-empty optional
    # Anthropic key, the editor and the pairing are asked again)
    result = run_setup(checkout, fake_bin, home, ["", "", "", "n", "n"])
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for expected in ("WRISTPULL_SITE is set", "WRISTPULL_STRAP_AUTHKEY is set", "OPENROUTER_API_KEY is set", "udev rule installed", "AppArmor profile installed",
                     "WRISTPULL_WEB_PASSWORD_HASH is set", ".env exists", "nothing to restore"):
        assert expected in out, expected
    assert env_file.read_text("utf-8") == text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash")
def test_setup_help():
    result = subprocess.run(["bash", str(REPO / "contrib" / "bin" / "wristpull-setup"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "the one place to start" in result.stdout
