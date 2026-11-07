# wristpull in Docker – concept

Status: spike on the real watch done 2026-11-07 (phase 0, results below); phase A (`cli`
service, ADB, container-aware TUI), phase B (MTP device layer, confirmed on the watch) and
phase C (udev rule and units for the container, host installation pending) implemented the same
day; results below. The web dashboard concept (`dashboard-concept.md`) still applies to the
`web` service; this document adds the `cli` service that runs everything else.

## Why

Today only the web dashboard runs in a container. Importing from the watch (USB/MTP), pulling
the phone (ADB), the TUI, export, report, coach and workouts need Python 3.14 and uv on the
host. The goal is **one image, two services**: the laptop and the later home server use the
same image, the host needs only Docker, and a published image (a separate follow-up) becomes
possible. The native uv installation stays a fully supported second way.

Decisions taken with the user (2026-11-07):

1. **ADB over USB pass-through; the adb server runs inside the `cli` container.** Nothing is
   installed on the host. The adb keys live in `<data_dir>/adb/` (bind mount), seeded once from
   the host's `~/.android/adbkey*` so the phone does not ask for a new authorisation. A second
   adb server on the host that holds the phone is detected and reported with a fix
   (`adb kill-server` on the host), never worked around. Wireless debugging
   (`[phone] serial = "ip:port"`) is an optional extra, not the default for the morning timer.
2. **libmtp through the command-line tools first** (`mtp-tools`: `mtp-detect`, `mtp-folders`,
   `mtp-files`, `mtp-getfile`, `mtp-sendfile`, `mtp-delfile`) behind a narrow interface in
   `src/wristpull/mtp.py`. Swappable for a `ctypes` binding to `libmtp` later without touching
   callers or `FakeMtp`. No FUSE mount in the container (would need `/dev/fuse` and
   `CAP_SYS_ADMIN`).
3. **`cli` uses `network_mode: host`** so the coach reaches Ollama on `http://localhost:11434`
   without any change to the host's Ollama configuration. `web` stays isolated in the Compose
   network (`host-gateway` for Ollama). Still forbidden: `OLLAMA_HOST=0.0.0.0` and `adb -a`.

## Target picture

```
services:
  web:   as before: long-running, behind Caddy, NO USB, config read-only, exactly one worker
  cli:   on demand: docker compose run --rm cli wristpull <command>
         profile "cli", TTY, network_mode: host, USB pass-through (/dev/bus/usb +
         device_cgroup_rules "c 189:* rmw"), data rw, config rw (the TUI settings screen
         writes it), adb keys from <data_dir>/adb
```

The image carries the extras `web`, `coach`, `tui` plus the Debian packages `adb`, `mtp-tools`
and `nano`. `WRISTPULL_IN_CONTAINER=1` marks the container; `WRISTPULL_DATA_DIR=/data` tells the
CLI where the bind-mounted data directory is (the host config still says `~/Health/wristpull`).

**Why two services:** `c 189:*` grants access to *every* USB device (keyboard, sticks, the
phone). That is acceptable for a short-lived command the user starts, but never for the
network-reachable `web` container.

## Spike results (phase 0, fenix 6X Pro, firmware 28.02, 2026-11-07)

Throw-away container `debian:trixie-slim` + `mtp-tools` (libmtp 1.1.22) + `adb`, run as
UID 1000 with `-v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw'`, no
`privileged`. Verbatim captures (serials replaced) are in `tests/fixtures/mtp/`.

| Step | Result |
|---|---|
| `mtp-detect` | works, 2.0 s; `Model: fenix 6X Pro`, `Serial number: 0000…` (the USB serial, **not** the unit id) |
| `mtp-folders` | 4.0 s; one storage `Primary`, ids for `GARMIN`, `Activity`, `Monitor`, `Sleep`, `Metrics`, `HRVStatus`, `Workouts`, `NewFiles`, `Schedule`; lines are `<id>\t<indent><name>` after `Storage:`; dozens of `MTP extended association type … encountered` noise lines on stdout |
| `mtp-files` | 6.8 s for all 250 objects; blocks `File ID / Filename / File size / Parent ID / Storage ID / Filetype`; **no modification time** |
| `mtp-getfile <id> <dest>` | 6.1 s per file; SHA-256 of an archived activity is **identical** to the gvfs import; a wrong id → rc 1, `Error getting file from MTP device.` |
| `mtp-sendfile <local> /GARMIN/NewFiles` | 6.1 s; the second argument is the **folder**, the file keeps the local basename; success only visible as `New file ID: <n>` on stdout. A missing folder prints `Parent folder could not be found, skipping` with **rc 0** |
| `mtp-delfile -n <id>` | 5.9 s, `Deleting <id>`; a second delete → rc 1, `Failed to delete file:<id>` + `PTP Layer error 2002` on stderr |
| `GarminDevice.xml` | fetched by id (48 KB); `<Id>` is the unit id used in `workout_pushes.watch_serial` |
| Host holds the device (`gio mount` on the host) | all tools print `libusb_claim_interface() reports device is busy, likely in use by GVFS or KDE MTP device handling already` + `Unable to open raw device 0` on **stderr**, while `mtp-folders` still says `Successfully connected` / `OK.` with rc 0 |
| No access to the device node | `libusb_open() failed!` + `LIBMTP PANIC: Unable to initialize device` on stderr, rc 0, empty tree |
| Watch unplugged | `No raw devices found.` / `no devices found` on stdout, rc 0 (from the libmtp sources; capture pending) |
| UID 1001 (no `uaccess` ACL) | worked on this laptop only because a third-party rule (`horizon_hid.rules`) sets `MODE="0666"` for all USB devices – not portable |

Conclusions:

- The CLI tools are reliable and fast enough: every call re-enumerates the device (~6 s per
  get/send/delete, ~11 s for one folder+file snapshot). A daily import of a handful of files
  takes well under a minute; the first import of 36 activities about four minutes. The
  `ctypes` fallback stays an option behind the same interface (it would also give
  modification times).
- **Exit codes are useless.** The parsers decide on stdout/stderr markers: `No raw devices` /
  `no devices found` → no device; `libusb_claim_interface() … busy` / `Unable to open raw device`
  / `libusb_open() failed` → device held or no permission; `New file ID:` → sent; `Failed to`
  → delete failed; the `MTP extended association` lines are noise.
- The `mtp-detect` serial is the USB serial; the unit id for `watch_serial` comes from
  `GARMIN/GarminDevice.xml`, fetched once per command.
- Set `LANG=C.UTF-8` in the image: the tools warn about a missing UTF-8 locale otherwise.
- Device permissions must not rely on the laptop's `0666` quirk or on the `uaccess` ACL (which
  exists only while a desktop session is active): the contrib udev rule sets
  `GROUP="plugdev", MODE="0660"` for vendor `091e`, and the `cli` service adds that group
  (`WRISTPULL_USB_GID`, default 46).
- The host grabs the device only when something opens it (gvfs mount, Dolphin/kiod6). The
  volume monitor alone does not. The contrib rule clears `ID_MTP_DEVICE` and
  `ID_MEDIA_PLAYER` for `091e` *after* `70-uaccess.rules` so the desktop no longer offers the
  watch; with that rule the native gvfs path on the same machine needs `[garmin] access = "gvfs"`
  plus a manual `gio mount` (verified in phase C).

## Phase B on the real watch (2026-11-07, `docker compose run --rm cli wristpull …`)

| Command | Result | Time |
|---|---|---|
| `status` / `mount` | `connected over MTP (fenix 6X Pro, no mount needed)`; mount reports the device only | 2 s |
| `import garmin --dry-run` | 44 FIT files listed, 36 skipped by name+size, 8 would be fetched, nothing downloaded | 10 s |
| `import garmin` | 8 files fetched and parsed, SHA-256 of `MB795515.FIT` identical to the spike's `mtp-getfile` copy; staging directory empty afterwards | 45 s |
| `import garmin` again | 44 already archived, 0 new | 10 s |
| `workout watch` (empty) | `0 active + 0 pending … (mtp://GARMIN on fenix 6X Pro)` | 11 s |
| `workout push --upcoming` | one file to `GARMIN/NewFiles`, `target` = `mtp://GARMIN/NewFiles/<file>`, unit id from `GarminDevice.xml` | 29 s |
| `workout watch` | lists it as pending, origin wristpull | 17 s |
| `workout remove --yes Ergometer` | backup in `workouts/watch-backup/`, file deleted, re-listing without a second fetch | 32 s |
| push again, unplug, plug in, `workout verify` | the watch imported the file as `GARMIN/Workouts/Ergometer_workout.fit`; verify matches it by name and time_created and marks the push verified | 16 s |

Every call opens the device again, so a command costs 2 s (detect) + 4 s (folders) + 6–7 s (files)
plus about 6 s per file transferred. Fine for the daily routine; a ctypes binding would cut the
per-call overhead if it ever matters.

## Phase C on this laptop (2026-11-07)

With `91-wristpull-garmin-docker.rules` installed and the `-docker` user units copied to
`~/.config/systemd/user/` (timers not enabled):

- Plugging the watch in started `wristpull-import-docker.service` by itself. The first run failed
  with "no MTP device found" because the fenix enumerates itself twice right after plugging in
  (USB device number +2); the second start imported 46 files, 2 new, in 20 s. The unit now waits
  8 s before starting.
- The device node belongs to `root:plugdev` (mode still `0666` here because of the laptop's
  `horizon_hid.rules`; on a clean host it is `0660`).
- `udevadm test` shows the rule clearing `ID_MTP_DEVICE` and `ID_MEDIA_PLAYER` and adding
  `SYSTEMD_USER_WANTS`; `gio mount -li` no longer lists the watch as a volume, so gvfs and KDE
  cannot grab it. `udevadm info` of the running device may still print the old properties from
  the udev database of the first enumeration; the behaviour is what counts.
- Open: the phone was not visible on USB during the session (no `lsusb` entry), so `pull phone`
  from the container and the host-adb-server conflict message are confirmed only with fakes.
  A verbatim "watch unplugged" capture for the fixtures is still missing.

## Guiding principles

- Raw files stay immutable and the watch read-only, exactly as on the gvfs path: the MTP layer
  writes only workout files into `GARMIN/NewFiles/` or `Workouts/` (never overwriting) and
  deletes only top-level workout files there after a backup.
- `web` keeps exactly one worker, no USB, config read-only; `cli` runs as the host UID/GID so
  files in the data directory keep their owner.
- Tests never touch hardware: an autouse fixture blocks the real `mtp-tools` runner the same
  way it blocks the real `Adb`; tests inject `FakeMtp` (`tests/fake_mtp.py`) or a runner that
  renders the fixture texts.
- Linux only: Docker Desktop on macOS and Windows does not pass USB through. The phone can
  still be reached over wireless ADB there.
- Concurrent writers: WAL covers the host CLI, `cli` and `web` on the same bind mount; imports
  keep transactions short.

## Waves

- **Phase 0 – spike** (done 2026-11-07): see above; fixtures captured.
- **Phase A – `cli` service without the watch:** image with `tui`, `adb`, `mtp-tools`, `nano`;
  `cli` service; `WRISTPULL_DATA_DIR`; adb keys in `<data_dir>/adb`; host adb-server conflict
  message; wireless ADB via `[phone] serial = "ip:port"`; `xdg-open` replaced by a notice with
  the host path; wrapper `contrib/bin/wristpull`.
- **Phase B – MTP device layer:** `mtp.py` (parsers, `MtpTools`, `FakeMtp`), import over a
  staging directory with name+size prefilter before the SHA-256 dedup, push/verify/watch/remove
  through a small `WatchFiles` abstraction (`workouts/device.py`), `[garmin] access = auto | gvfs | mtp`,
  `status` and `mount` aware of MTP.
- **Phase C – automation on the host:** udev rule for the container variant (group, mode, hide
  from the desktop, start the user unit), systemd units that run `docker compose run --rm cli …`.
- Not part of this: publishing the image (GHCR), `wristpull ship` / the server move,
  `wristpull doctor`.
