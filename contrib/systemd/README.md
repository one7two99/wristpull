# Automation templates

User-level systemd units for the Docker `cli` service. wristpull needs none of them: the guided
`contrib/bin/wristpull-morning` covers the daily routine on a laptop. They are templates for a
machine that runs unattended, such as the planned hub.

| Unit | What it does |
|---|---|
| `wristpull-import-docker.service` | oneshot USB import, started by `../udev/91-wristpull-garmin-docker.rules` when the watch is plugged in |
| `wristpull-pull-docker.service` / `.timer` | `wristpull pull phone` every morning at 06:45 (and 15 minutes after boot) |
| `wristpull-coach-daily-docker.service` / `.timer` | `wristpull coach daily` at 07:00, after the pull |

Copy what you want to `~/.config/systemd/user/`, check the compose path
`%h/dev/wristpull/compose.yaml`, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now wristpull-pull-docker.timer wristpull-coach-daily-docker.timer   # the optional timers
systemctl --user list-timers 'wristpull-*'
journalctl --user -u wristpull-import-docker.service -n 50
loginctl enable-linger $USER     # timers and the USB import without a logged-in desktop session
```

- The import service needs the udev rule (device node readable for `plugdev`, desktop kept away
  from the watch); the container opens the watch with the MTP tools, no desktop session needed.
- The pull timer needs the phone reachable over ADB; a failed run is logged and retried the next day.
- The coach's cloud backends take their API key from `.env` next to the compose file.
- The units run `docker compose run --rm -T cli …`: your user must be in the `docker` group. They
  are *user* units, so they do not depend on `docker.service`; the timers' `OnBootSec` covers the
  start-up gap.
- The `cli` service runs under the AppArmor profile `contrib/apparmor/wristpull-cli` (install it
  once, see that README); without it Docker refuses to start the container.

Order matters for Garmin watches: import over USB **before** any phone app syncs, because the watch
deletes monitoring files once a sync client has fetched them (see the README).

Workouts: while the watch is on the cable, `wristpull workout push --upcoming` copies the planned
workouts (Markdown files under `<data_dir>/workouts/`) to `GARMIN/NewFiles/`; the watch imports them
when the cable is unplugged. The import service shows the second `ExecStart=` line for it (commented
out); it exits 2 when nothing is pending, so set `SuccessExitStatus=2` too.
