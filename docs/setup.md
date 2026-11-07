# Setting up wristpull on a new machine (Docker only)

The whole stack runs from the Docker image: dashboard, CLI, TUI, the watch over USB, the phone over
ADB, the strap over Bluetooth. The host needs neither Python nor uv. Everything below is Linux
(USB and Bluetooth pass-through); tested on Ubuntu 26.04 with Docker 29 and Compose v5.

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
Debian/Ubuntu). The rule also starts `wristpull-import-docker.service` on plug-in if you install
the unit from `contrib/systemd/` (optional; `loginctl enable-linger $USER` for runs without a
desktop session).

## 5. Strap over Bluetooth (AppArmor profile, auth key)

```sh
sudo install -m 0644 contrib/apparmor/wristpull-cli /etc/apparmor.d/wristpull-cli
sudo apparmor_parser -r -W /etc/apparmor.d/wristpull-cli        # once; /etc/apparmor.d loads it at boot
docker compose run --rm cli wristpull strap scan                 # the band must not be connected to a phone
docker compose run --rm cli wristpull strap pair --authkey 0x<32 hex> --address <AA:BB:CC:DD:EE:FF>
docker compose run --rm cli wristpull pull strap
```

The `cli` service runs under that profile (Docker's default plus system D-Bus); without it Docker
refuses to start the container. The auth key is the band's 16-byte key: in Gadgetbridge under the
band's settings → *Auth key* (or in a Gadgetbridge backup, `preferences/device_<MAC>.json`), or
from the Zepp account with [huami-token](https://codeberg.org/argrento/huami-token). `pair` stores
it in `<data_dir>/strap/authkey` (mode 0600) and writes the address into the config.

## 6. Dashboard (phone in the home network)

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"           # -> WRISTPULL_WEB_SECRET in .env
docker compose run --rm cli wristpull serve --hash-password              # -> WRISTPULL_WEB_PASSWORD_HASH in .env
# WRISTPULL_SITE in .env: the LAN address Caddy serves, e.g. https://192.168.1.10:8443, https://localhost:8443
docker compose up -d
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./wristpull-root.crt   # install on the phone once
```

Keep the `caddy_data` volume across rebuilds: it holds that certificate authority. Details and the
Ollama notes are in the README, "Web dashboard".

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

`contrib/systemd/` has optional timers and the plug-in import service in Docker variants; the
coach's cloud backends take their API keys from `.env`.

## 9. Updating

```sh
git pull && docker compose build && docker compose up -d web
```

After a Docker upgrade that changes its default AppArmor profile, compare
`contrib/apparmor/wristpull-cli` with moby's template and reload it.

## 10. Moving the data to another machine

Everything wristpull knows lives in three places; nothing is stored inside the containers:

| What | Where | Holds |
|---|---|---|
| data directory | `~/Health/wristpull` (`WRISTPULL_DATA`) | `raw/` (every device file, immutable), `wristpull.sqlite`, `strap/authkey`, `adb/` (phone authorisation), `coach/` (goal profile, prompts), `workouts/`, `exports/`, `reports/`, `staging/` (may be empty) |
| config | `~/.config/wristpull/config.toml` (`WRISTPULL_CONFIG`) | timezone, folders, `[strap] address`, coach and UI settings |
| `.env` next to `compose.yaml` | the checkout | UID/GID, the two paths above, dashboard secrets, API keys |

Steps, with the old machine still available:

```sh
# on the old machine: stop the dashboard so the database is not written while copying
cd ~/dev/wristpull && docker compose down
rsync -a --delete ~/Health/wristpull/ newhost:~/Health/wristpull/     # keeps the 0600 of strap/authkey and the read-only raw files
scp ~/.config/wristpull/config.toml newhost:~/.config/wristpull/config.toml
scp ~/dev/wristpull/.env newhost:~/dev/wristpull/.env                 # contains secrets: scp/rsync only, never mail or chat
```

On the new machine, after steps 1 to 5 of this guide (packages, checkout, build, udev rule,
AppArmor profile):

- `.env`: adjust `UID`/`GID` (`id -u`, `id -g`) and the two paths if the user or home differ;
  `WRISTPULL_SITE` to the new LAN address. Keep `WRISTPULL_WEB_SECRET` to stay logged in on the phone.
- `config.toml`: `[storage] data_dir` must be the host path of the copied data directory; everything
  else (timezone, `[strap] address`, coach backend) stays as it was.
- The data directory must be owned by the new user (`chown -R $USER:$USER ~/Health/wristpull`
  if the UID changed); the files under `raw/` stay read-only.
- `docker compose run --rm cli wristpull status` shows the same tables and raw-file counts as on
  the old machine; `wristpull strap info` the same sync state, so the next `pull strap` only brings
  what the band recorded since. The band's auth key works on every host, but the band holds one
  connection: the old machine (or the phone) must not be talking to it.
- The phone keeps trusting the copied adb keys (`adb/`); without them it asks once for a new
  authorisation.
- Caddy's certificate authority is **not** copied (it lives in the Docker volume `caddy_data`): the
  new machine mints a new one, so install the new `wristpull-root.crt` on the phone (step 6) and
  remove the old one.
- Optional on the old machine afterwards: `docker compose down -v` removes its containers and the
  Caddy volume; the data directory can stay as a backup.

Without the old machine (restoring from a backup): the same files from the backup, then the same
checks. A backup therefore means the data directory plus the config file plus `.env`; the repository
is on GitHub.
