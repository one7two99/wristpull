# Setting up wristpull on a new machine (Docker only)

The whole stack runs from the Docker image: dashboard, device agent, CLI, TUI, the watch over USB,
the phone over ADB, the strap over Bluetooth. The host needs neither Python nor uv. Everything below is Linux
(USB and Bluetooth pass-through); tested on Ubuntu 26.04 with Docker 29 and Compose v5.
Other operating systems and the phones: [platforms.md](platforms.md).

## 0. The short way: `contrib/bin/wristpull-setup`

```sh
git clone https://github.com/one7two99/wristpull.git ~/dev/wristpull && cd ~/dev/wristpull
contrib/bin/wristpull-setup              # walks through steps 1 to 6 below and asks only for what is missing
contrib/bin/wristpull-setup --dry-run    # shows every command instead of running it
```

The script is the one place to start on a new machine: it checks Docker and the groups, offers to
restore an archive made with `wristpull-backup`, writes `.env` (UID/GID, paths, LAN address, a
generated session secret, the dashboard password hashed in the container, API keys and the band's
auth key typed without echo), builds the image, fills the config, installs the udev rule and the
AppArmor profile (each `sudo` command is shown and confirmed), starts the dashboard, exports Caddy's
root certificate and ends with `wristpull status`. Everything it asks for beyond UID/GID, paths and
the LAN address can also be set later on the dashboard's Settings page (start password `wristpull`). It is safe to run again: every step reports "ok"
for what is already done. It is plain bash, one function per step, written to be read.
The sections below are the same steps by hand, and the reference for what the script does.

## 1. Host packages and groups

```sh
# Docker Engine with the compose plugin (docker.com packages or the distribution's), then:
sudo usermod -aG docker $USER          # log out and in again
sudo apt install usbutils bluez        # lsusb for the morning script; bluetoothd for the strap
getent group plugdev || sudo groupadd plugdev
sudo usermod -aG plugdev $USER         # watch and phone device nodes (see the udev rule below)
```

The strap needs your user to be allowed to talk to BlueZ: a logged-in desktop session is enough,
otherwise `sudo usermod -aG bluetooth $USER`.

## 2. Checkout, data directory, config

```sh
git clone https://github.com/one7two99/wristpull.git ~/dev/wristpull && cd ~/dev/wristpull
mkdir -p ~/Health/wristpull ~/.config/wristpull
touch ~/.config/wristpull/config.toml          # compose bind-mounts this file; it must exist
cp .env.example .env                           # then edit: UID/GID (id -u, id -g), WRISTPULL_DATA, WRISTPULL_CONFIG
```

Moving from another machine? Copy the data instead of starting empty: see step 10.

## 3. Build and first run

```sh
docker compose build                           # one image, wristpull:latest, for `web` and `cli`
docker compose run --rm cli wristpull config --init    # fills the empty config file with the defaults
$EDITOR ~/.config/wristpull/config.toml        # [time] timezone, [storage] data_dir (host path), later [strap] address
docker compose run --rm cli wristpull status
```

`[storage] data_dir` keeps the host path (`~/Health/wristpull`); inside the container the data
directory is always `/data` (`WRISTPULL_DATA_DIR`). `contrib/bin/wristpull <command>` is a wrapper
for `docker compose run --rm cli wristpull <command>` that finds the compose file by itself.

## 4. Watch over USB (udev rule)

```sh
sudo cp contrib/udev/91-wristpull-garmin-docker.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# plug the watch in; the node should be crw-rw---- root plugdev, and the desktop no longer offers it as a drive
docker compose run --rm cli wristpull import garmin
```

`WRISTPULL_USB_GID` in `.env` is the `plugdev` GID (`getent group plugdev | cut -d: -f3`, 46 on
Debian/Ubuntu). The import on plug-in is the device agent's job (step 6, `[agent] import_on_plug`,
on by default); the udev rule's `wristpull-import-docker.service` from `contrib/systemd/` is the
older way for a machine without the agent and must not be installed next to it.

## 5. Strap over Bluetooth (AppArmor profile, auth key)

```sh
sudo install -m 0644 contrib/apparmor/wristpull-cli /etc/apparmor.d/wristpull-cli
sudo apparmor_parser -r -W /etc/apparmor.d/wristpull-cli        # once; /etc/apparmor.d loads it at boot
docker compose run --rm cli wristpull strap scan                 # the band must not be connected to a phone
echo 'WRISTPULL_STRAP_AUTHKEY=0x<32 hex>' >> .env                  # the band's auth key, next to the other secrets
docker compose run --rm cli wristpull strap pair --authkey 0x<32 hex> --address <AA:BB:CC:DD:EE:FF>   # checks it, writes the address
docker compose run --rm cli wristpull pull strap
```

The `cli` service runs under that profile (Docker's default plus system D-Bus); without it Docker
refuses to start the container. The auth key is the band's 16-byte key: in Gadgetbridge under the
band's settings → *Auth key* (or in a Gadgetbridge backup, `preferences/device_<MAC>.json`), or
from the Zepp account with [huami-token](https://codeberg.org/argrento/huami-token). It lives in
`.env` like the API keys (`WRISTPULL_STRAP_AUTHKEY`), never in the config file.

## 6. Dashboard (phone in the home network)

```sh
# WRISTPULL_SITE in .env: the LAN address Caddy serves, e.g. https://192.168.1.10:8443, https://localhost:8443
# Optional, the old way: WRISTPULL_WEB_SECRET and WRISTPULL_WEB_PASSWORD_HASH in .env
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
#   docker compose run --rm cli wristpull serve --hash-password
# Without them: log in with the start password "wristpull" and set your own at once; the secret is generated.
docker compose up -d                                                     # web, caddy and the device agent
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./wristpull-root.crt   # install on the phone once
```

Keep the `caddy_data` volume across rebuilds: it holds that certificate authority. Details and the
Ollama notes are in the README, "Web dashboard". `docker compose up -d` also starts the **device
agent** (`agent`), which runs the import and pull jobs the dashboard's Sync page starts; it needs
steps 4 and 5 (udev rule, AppArmor profile) like `cli`. Today shows "device agent online" when it
runs; `docker compose run --rm cli wristpull agent --status` prints the same from the terminal.

## 7. Phone over ADB (only if Gadgetbridge stays in use)

```sh
mkdir -p ~/Health/wristpull/adb && cp ~/.android/adbkey* ~/Health/wristpull/adb/   # if the phone already trusts this machine
docker compose run --rm cli wristpull pull phone        # otherwise the phone asks once to authorise the new key
```

Requirements on the phone: `docs/gadgetbridge.md`.

## 8. Daily routine

```sh
contrib/bin/wristpull-morning            # watch → strap → coach, each step through the container
contrib/bin/wristpull-morning strap      # one step only
```

Or from the browser: the dashboard's Sync page starts the same jobs on the device agent, with a
live log and the stop button.

`contrib/systemd/` has optional timers for the `cli` service (and the plug-in import service the
agent replaces); the coach's cloud backends take their API keys from `.env`.

## 9. Updating

```sh
git pull && docker compose build && docker compose up -d web agent
```

After a Docker upgrade that changes its default AppArmor profile, compare
`contrib/apparmor/wristpull-cli` with moby's template and reload it.

## 10. Moving the data to another machine

Everything wristpull knows lives in four places; nothing is stored inside the containers:

| What | Where | Holds |
|---|---|---|
| data directory | `~/Health/wristpull` (`WRISTPULL_DATA`) | `raw/` (every device file, immutable), `wristpull.sqlite`, `adb/` (phone authorisation), `coach/` (goal profile, prompts), `workouts/`, `exports/`, `reports/`, `staging/` (may be empty) |
| config | `~/.config/wristpull/config.toml` (`WRISTPULL_CONFIG`) | timezone, folders, `[strap] address`, coach and UI settings |
| `.env` next to `compose.yaml` | the checkout | UID/GID, the two paths above, `WRISTPULL_SITE`; optionally dashboard secrets, API keys, the band's auth key (`WRISTPULL_STRAP_AUTHKEY`) |
| secrets store | `~/Health/wristpull/secrets.json` (in the data directory, mode 0600) | what the Settings page saved: password hash, session secret, API keys, the band's auth key, the Ollama address; wins over `.env` |
| certificates | `deploy/certs/` in the checkout (git-ignored) | your own CA's certificate and key when `WRISTPULL_TLS` points at them (empty with Caddy's own CA) |

`wristpull backup` packs all of it into one archive and unpacks it on the other side. The quickest way
is the dashboard: **Backup page** → "Create a backup now", download the archive; on the new machine
`docker compose up -d` with a fresh `.env`, log in with the start password, "Restore a backup
instead", upload, restore, then `docker compose up -d` once more so the agent and Caddy get the
restored `.env` and certificates. The same on the command line:

```sh
# old machine (the dashboard may keep running)
cd ~/dev/wristpull && contrib/bin/wristpull-backup          # → ~/Health/wristpull/backups/wristpull-backup-<date>.tar.gz
# copy the archive over: it holds the secrets, so scp/rsync or a disk, never mail or chat
scp ~/Health/wristpull/backups/wristpull-backup-*.tar.gz newhost:

# new machine, after step 1 (Docker, groups) and the clone from step 2 – no .env, no config yet
git clone https://github.com/one7two99/wristpull.git ~/dev/wristpull && cd ~/dev/wristpull
contrib/bin/wristpull-restore ~/wristpull-backup-<date>.tar.gz          # builds the image, unpacks, writes .env and deploy/certs
docker compose build && docker compose up -d                            # dashboard
docker compose run --rm cli wristpull status                            # same counts as on the old machine
```

`wristpull-restore` takes the data directory and the config file as optional second and third
arguments (defaults `~/Health/wristpull`, `~/.config/wristpull/config.toml`) and refuses to touch
targets that already hold something (`--force`, anywhere on the line, lets the archive overwrite
files; nothing is ever deleted). It rewrites `UID`/`GID`, `WRISTPULL_DATA` and `WRISTPULL_CONFIG` in
`.env` and `[storage] data_dir` in the config for the new machine; everything else travels unchanged.
Then:

- `.env`: set `WRISTPULL_SITE` to the new LAN address and check `WRISTPULL_USB_GID`
  (`getent group plugdev`). `WRISTPULL_WEB_SECRET` is kept, so the phone stays logged in.
- Steps 4 and 5 (udev rule, AppArmor profile) are host installs and must be repeated.
- `wristpull strap info` shows the same sync state, so the next `pull strap` only brings what the
  band recorded since. The band holds one connection: the old machine (or the phone) must not be
  talking to it.
- The phone keeps trusting the copied adb keys (`adb/`); without them it asks once for a new
  authorisation.
- Caddy's own certificate authority is **not** in the archive (it lives in the Docker volume
  `caddy_data`): the new machine mints a new one, so install the new `wristpull-root.crt` on the
  phone (step 6) and remove the old one. Own certificates from `deploy/certs/` are in the archive
  and come back; `WRISTPULL_TLS` in the restored `.env` keeps pointing at them.
- Optional on the old machine afterwards: `docker compose down -v` removes its containers and the
  Caddy volume; the data directory can stay as a backup.

By hand instead of the archive: `rsync -a` the data directory, copy the config file and `.env`, and
adjust the same values yourself (`chown -R $USER:$USER` the data directory if the UID changed; the
files under `raw/` stay read-only).

### Backups

The dashboard's Backup page and `contrib/bin/wristpull-backup [DIR | FILE.tar.gz]` write the same
backup: one `wristpull-backup-<date>.tar.gz` under `~/Health/wristpull/backups/` with the data
directory (without `staging/` and `backups/`, the database as a consistent copy made with SQLite's
backup API, so nothing has to be stopped), the config file, `.env` and the files of `deploy/certs/`,
plus `MANIFEST.json` and `SHA256SUMS` (`wristpull backup show <archive>` prints the manifest; the
page shows it per archive). The archive holds the secrets and is written with mode 0600: download it
or copy it to an external disk or another machine, never in mail or chat. Restoring is the Backup
page (upload or pick an archive, confirm with the password; the archive's files overwrite, the live
database is overwritten in place, nothing is deleted, then "Restart the dashboard" and
`docker compose up -d` on the host) or `contrib/bin/wristpull-restore <archive>` into empty targets
(`--force` over the existing ones). The database schema migrates itself on the first connect, so an
older archive opens fine, but whatever was imported after it has to be imported again
(`import garmin-export`, `pull strap`, `import garmin`: device files are re-fetched only where the
device still has them, which is why the raw files are the part worth keeping).

Archives on this laptop, all under `~/Health/wristpull-backups/`:

| Archive | Taken | Holds | Restore |
|---|---|---|---|
| `wristpull-backup-2026-11-08-0821.tar.gz` (111 MB, 15,507 files) | 2026-11-08 morning, with `contrib/bin/wristpull-backup` | data directory, config, `.env` (strap key, API keys), schema version 6 | `contrib/bin/wristpull-restore <archive>`; verified the same day on a scratch clone |
| `wristpull-backup-2026-11-07.tar.gz` (5.4 MB, 230 files) | 2026-11-07 morning by hand, before the Garmin Connect export import, schema version 3 | raw files, workouts, coach, exports, reports, adb keys, a consistent `wristpull.sqlite`, `SHA256SUMS` | unpack, then its `RESTORE.md` (`rsync` over the data directory); afterwards re-run `import garmin-export` and `pull strap` (the strap over Bluetooth did not exist yet, nor the `.env` key) |
