# Platforms: which way for which machine

wristpull is developed on Linux and for Linux, and the Docker stack is the primary way to run it:
one image for the dashboard and every command, the devices reached from the container. That is
also the shape of the planned **wristpull hub**, a Raspberry Pi that runs this stack, collects the
devices' data on its own and serves the dashboard to every phone and laptop in the house. Until the
hub exists, this page says what works where, what each path imports, and what has to be installed
for it. Nothing here excludes macOS, Windows or an iPhone: the same Python package runs natively,
only the device access differs per operating system.

Status words used below: **verified** means it runs in daily use on the author's machines;
**expected** means the libraries support it and nothing in the code is known to prevent it, but
nobody has tried it yet (reports welcome, see the issue templates).

## 1. The strategy in three sentences

1. **Linux + Docker** is the reference. It is what `contrib/bin/wristpull-setup` installs, what the
   tests and the daily routine cover, and what the hub will run.
2. **Native installs** (uv, same package, same commands) exist for Linux without Docker and for
   macOS and Windows; there the device access is the only part that differs.
3. **Phones** are viewers of the dashboard. An Android phone with Gadgetbridge can additionally be a
   data source; an iPhone cannot, and does not need to be.

## 2. What can be imported, and over which path

| Data | Path | Command | What arrives |
|---|---|---|---|
| Garmin watch (fenix 6 and similar) | USB, MTP | `import garmin` | activities, daily monitoring, sleep, metrics, HRV as FIT files from the watch itself |
| Garmin watch | copied folder | `import garmin --path DIR` | the same, from a copy of `GARMIN/` made with any file manager |
| Garmin Connect account export | zip from the portal | `import garmin-export` | the whole account history: FIT files, daily summaries, nights, activity names, VO2max, weight |
| Amazfit Helio Strap (Zepp OS band) | Bluetooth LE, the band's auth key | `pull strap` | steps, heart rate, stress, SpO2, HRV, resting heart rate, respiration, sleep, max heart rate, PAI, workouts |
| Garmin watch and strap via Gadgetbridge | Android phone over ADB (USB debugging) | `pull phone` | Gadgetbridge's database export plus the FIT files it fetched |
| Gadgetbridge export, already copied | folder or database file | `import gadgetbridge` | the same as above, without the phone attached |
| Workouts to the watch | USB, MTP (write) | `workout push` | planned workouts as FIT files into `GARMIN/NewFiles/` |

Only the first column's devices need platform-specific access. The Connect export and the copied
folders work everywhere, and the strap's Bluetooth path needs no phone at all.

## 3. Linux

### Docker stack (primary, verified)

Everything runs in two containers from one image: `web` (dashboard behind Caddy) and `cli`
(every command, with the USB bus, the host's Bluetooth daemon and the host network). Data stays in
`~/Health/wristpull`, config in `~/.config/wristpull`, secrets in `.env`; the only Docker volumes
hold Caddy's certificate authority.

| Needs | Why |
|---|---|
| Docker Engine with the compose plugin | the stack |
| groups `docker`, `plugdev`, `bluetooth` (or a desktop session) | containers, USB device nodes, BlueZ |
| `usbutils`, `bluez` | the morning script waits for the watch with `lsusb`; `bluetoothd` for the strap |
| udev rule `contrib/udev/91-wristpull-garmin-docker.rules` | device node for `plugdev`, hides the watch from the desktop's MTP handling, plug-in import |
| AppArmor profile `contrib/apparmor/wristpull-cli` | Docker's default profile denies the D-Bus calls Bluetooth needs |

`contrib/bin/wristpull-setup` installs all of it and asks only for what is missing. Daily use:
`contrib/bin/wristpull-morning`. Verified on Ubuntu 26.04 with Docker 29 and Compose v5.

Paths: all of section 2. Watch over MTP (libmtp tools inside the image), strap over BlueZ,
Gadgetbridge over ADB (adb inside the image, keys in `<data_dir>/adb/`), workout push over MTP.

### Native with uv (verified, the developer path)

```sh
uv sync --extra tui --extra coach --extra web --extra ble
uv run wristpull status
```

| Needs | For |
|---|---|
| Python 3.14, uv | the package |
| `mtp-tools` (libmtp) | the watch over USB; nothing else may hold it (install the udev rule, or unmount the watch in the desktop first) |
| `bluez`, user in `bluetooth` or a desktop session | the strap |
| `adb` (`android-tools-adb`), keys in `~/.android` | the phone |

Paths: all of section 2. The native path is what the Docker image runs inside anyway, so it is
exercised by every container run.

## 4. macOS (expected, not verified)

Docker Desktop on macOS does not pass USB or Bluetooth into containers, so the stack can only serve
the dashboard there. The native install is the way:

```sh
brew install uv libmtp android-platform-tools
uv sync --extra tui --extra coach --extra web --extra ble
```

| Path | How | Status |
|---|---|---|
| Garmin over USB | Homebrew's libmtp tools (`mtp-files`, `mtp-getfile`); nothing else may hold the watch (no Android File Transfer running) | expected |
| Garmin, copied folder | copy `GARMIN/` with Android File Transfer or OpenMTP, then `import garmin --path` | expected |
| Garmin Connect export | `import garmin-export` | expected (pure Python) |
| Strap over Bluetooth | bleak uses CoreBluetooth; macOS asks once for Bluetooth permission for the terminal app | expected |
| Gadgetbridge over ADB | `android-platform-tools` | expected |
| Workout push | over libmtp's `mtp-sendfile`, same requirement as the import | expected |
| TUI, dashboard, coach, export, backup | native, `uv run wristpull serve` for the dashboard (Caddy and the Docker stack optional) | expected |

## 5. Windows (expected, not verified)

Windows exposes MTP devices only through Explorer, not as a path or through libmtp in a practical
way, so the watch is read from a copy. Everything else is the same package.

```powershell
winget install astral-sh.uv Google.PlatformTools
uv sync --extra tui --extra coach --extra web --extra ble
```

| Path | How | Status |
|---|---|---|
| Garmin over USB | copy `This PC\fenix 6X Pro\Primary\GARMIN` to a folder, then `import garmin --path` | expected |
| Garmin Connect export | `import garmin-export` | expected |
| Strap over Bluetooth | bleak uses WinRT; the band must not be paired in Windows' own Bluetooth settings | expected |
| Gadgetbridge over ADB | platform-tools, USB driver for the phone | expected |
| Workout push | `workout push --dry-run` writes the FIT files to `<data_dir>/workouts/out/`; copy them into `GARMIN\NewFiles` in Explorer | expected |
| TUI, dashboard, coach, export, backup | native (Windows Terminal for the TUI); Docker Desktop with WSL 2 optional for the dashboard | expected |

Known rough edges to expect: file modes (the archive marks raw files read-only, which maps to the
read-only attribute), path separators in messages, and the host scripts under `contrib/bin/` are
shell scripts for Linux (WSL runs them, PowerShell does not).

## 6. Phones

### Android

- **Viewer:** the dashboard in any browser over the home network; Caddy's root certificate is
  installed once (README, "Web dashboard").
- **Data source (optional):** Gadgetbridge with the watch and the strap, USB debugging on, then
  `pull phone` over ADB from a Linux, macOS or Windows machine (`docs/gadgetbridge.md`). This is
  the way for anyone who wants the phone to keep syncing the devices during the day. It is not
  needed when the watch goes on the cable and the strap is in Bluetooth range of the computer.

### iPhone

- **Viewer:** the dashboard in Safari, the certificate installed as a profile.
- **Data source:** none. Gadgetbridge does not exist for iOS, and wristpull never talks to Garmin's
  or Zepp's cloud. Nothing is lost: the watch over USB and the strap over Bluetooth are read
  directly, and the Connect export brings the history.

## 7. The hub (planned)

A Raspberry Pi running the Linux Docker stack, with the watch docked on its USB and the strap in
Bluetooth range: imports run on a schedule on the hub, the dashboard is served to every device in
the house, backups go to a disk or another machine with `wristpull backup`. Then no laptop or phone
needs device access any more, whatever it runs, which is why the Docker stack stays the reference
and the native installs are the bridge until then. Concept: `docs/history/Helio-Strap-BLE-Konzept.md`,
phase 3; it is a separate project.

## 8. Summary

| Machine | Recommended | Devices | Also |
|---|---|---|---|
| Linux | Docker stack (`wristpull-setup`) | watch USB, strap BLE, phone ADB, workout push | native uv for development |
| Linux without Docker | native uv with `mtp-tools` | the same | `import garmin --path` from a copied folder |
| macOS | native uv | watch via libmtp (expected), strap via CoreBluetooth (expected), phone ADB | Docker Desktop for the dashboard only |
| Windows | native uv | watch from a copied folder, strap via WinRT (expected), phone ADB | WSL 2 for the shell scripts |
| Android phone | dashboard viewer | optional data source with Gadgetbridge | |
| iPhone | dashboard viewer | none, none needed | |
| Raspberry Pi hub | Docker stack, planned | all, on the hub | every other machine only views |
