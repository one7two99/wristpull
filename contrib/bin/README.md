# contrib/bin

`wristpull-setup` – the one place to start on a new machine (or to check an existing one):

```sh
contrib/bin/wristpull-setup              # 11 steps: host checks, optional restore, .env, image, dashboard
                                         # password, keys, config, udev rule, AppArmor profile, start, status
contrib/bin/wristpull-setup --dry-run    # show every command, write nothing
```

Plain bash, one function per step, every change goes through `run()` (shown, skipped in a dry run)
or `sudo_run()` (shown and confirmed). Secrets are typed without echo and land only in `.env`
(mode 0600). Values already in `.env` are not asked again (a value copied unchanged from
`.env.example` counts as missing). `tests/test_contrib_scripts.py` runs it with a fake `docker` and
`sudo` on the PATH; `WRISTPULL_UDEV_DIR` and `WRISTPULL_APPARMOR_DIR` point the two installs at a
temp dir there.

`wristpull` – a shell wrapper that runs the CLI from the Docker image:

```sh
contrib/bin/wristpull status     # = docker compose run --rm cli wristpull status
contrib/bin/wristpull tui
# optional, to have it on the PATH: cp contrib/bin/wristpull ~/.local/bin/
```

It finds the `compose.yaml` of its checkout by itself (`WRISTPULL_COMPOSE` overrides), and drops the
pseudo-terminal (`run -T`) when stdin or stdout is not a terminal (timers, pipes). The `.env` next
to the compose file provides UID/GID, the data directory and the config file. See README
"Running everything in Docker".

`wristpull-morning` – the morning routine as a guided script (no timers, nothing runs unattended):

```sh
contrib/bin/wristpull-morning              # 1 watch: plug in → import (+ optional workout push) → unplug
                                           # 2 strap: pull the band over Bluetooth (in range, not on the phone)
                                           # 3 coach: optional daily check
contrib/bin/wristpull-morning strap coach  # only some steps
contrib/bin/wristpull-morning watch phone  # the Gadgetbridge way: sync in the app, plug the phone in, pull phone
```

It waits for the devices with `lsusb` (`WRISTPULL_WATCH_VENDOR=091e`, `WRISTPULL_PHONE_VENDOR=3310`
for a Mudita Kompakt; look yours up with `lsusb`), waits for a running plug-in import service before
touching the watch, and runs everything through the Docker `cli` service (`WRISTPULL_CMD="uv run
wristpull"` switches to the native installation).

`wristpull-backup` and `wristpull-restore` – the whole state as one tar.gz and back:

```sh
contrib/bin/wristpull-backup                        # → ~/Health/wristpull/backups/wristpull-backup-<date>.tar.gz
contrib/bin/wristpull-backup /media/disk/wp.tar.gz  # a directory or a file name instead
contrib/bin/wristpull-restore wp.tar.gz [~/Health/wristpull] [~/.config/wristpull/config.toml] [--force]
```

`wristpull-backup` runs `wristpull backup create` in the `cli` service; compose mounts the checkout's
`.env` and `deploy/certs/` into it, so both are in the archive, and the dashboard may keep running. The
default folder is the one the dashboard's Backup page uses (`<data_dir>/backups/`, never part of an
archive). `wristpull-restore` builds the image when it is missing and runs `wristpull backup restore`
in a plain container (compose would need the `.env` the restore writes, and the AppArmor profile is not
installed yet on a fresh machine); it writes `.env`, the config, the data directory and the certificate
files. The same restore works without the script: `docker compose up -d` on a fresh `.env`, log in with
the start password and upload the archive on the Backup page.

What an archive holds: `docker compose run --rm -v "$f:/b.tar.gz:ro" cli wristpull backup show /b.tar.gz`.
