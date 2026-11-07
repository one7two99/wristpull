# Automation templates

User-level systemd units for the daily routine, in two variants:

| Native (uv checkout) | Docker (`cli` service) |
|---|---|
| `wristpull-pull.service` / `.timer` | `wristpull-pull-docker.service` / `.timer` |
| `wristpull-coach-daily.service` / `.timer` | `wristpull-coach-daily-docker.service` / `.timer` |
| `wristpull-import.service` | `wristpull-import-docker.service` |

Copy one set to `~/.config/systemd/user/`, replace `/home/alice/dev/wristpull` (native) or check the
compose path `%h/dev/wristpull/compose.yaml` (Docker), then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now wristpull-pull.timer wristpull-coach-daily.timer          # native
systemctl --user enable --now wristpull-pull-docker.timer wristpull-coach-daily-docker.timer   # Docker
systemctl --user list-timers 'wristpull-*'
journalctl --user -u wristpull-pull-docker.service -n 50
loginctl enable-linger $USER     # timers and the USB import without a logged-in desktop session
```

- The pull timer runs `wristpull pull phone` every morning at 06:45 (and 15 minutes after boot).
  The phone must be reachable over ADB; a failed run is logged and retried the next day.
- The coach timer runs `wristpull coach daily` at 07:00, after the pull. Cloud backends need their
  API key: native in `~/.config/environment.d/` or a drop-in, Docker in `.env` next to the compose file.
- The import service is a oneshot for the USB import, started by the matching udev rule in `../udev/`
  when the watch is plugged in. The native one needs your desktop session (gio mounts the MTP device);
  the Docker one does not (the container opens the watch with the MTP tools), but the Docker udev rule
  must be installed so the desktop does not hold the device and the node is readable for `plugdev`.
- Docker units run `docker compose run --rm -T cli …`: your user must be in the `docker` group, and
  the units are *user* units, so they do not depend on `docker.service`; the timers' `OnBootSec`
  covers the start-up gap.

Order matters for Garmin watches: import over USB **before** any phone app syncs, because the watch
deletes monitoring files once a sync client has fetched them (see the README).

Workouts: while the watch is on the cable, `wristpull workout push --upcoming` copies the planned
workouts (Markdown files under `<data_dir>/workouts/`) to `GARMIN/NewFiles/`; the watch imports them
when the cable is unplugged. Both import services show the second `ExecStart=` line for it (commented
out in the Docker one); it exits 2 when nothing is pending, so set `SuccessExitStatus=2` too.

The Docker variants start the `cli` service, which runs under the AppArmor profile `contrib/apparmor/wristpull-cli` (install it once, see that README); without it Docker refuses to start the container.
