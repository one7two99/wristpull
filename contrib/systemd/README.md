# Automation templates

User-level systemd units for the daily routine. Copy them to `~/.config/systemd/user/`, replace
`/home/alice/dev/wristpull` with your checkout, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now wristpull-pull.timer wristpull-coach-daily.timer
systemctl --user list-timers 'wristpull-*'
journalctl --user -u wristpull-pull.service -n 50
```

- `wristpull-pull.timer` runs `wristpull pull phone` every morning at 06:45 (and 15 minutes after boot).
  The phone must be reachable over ADB; a failed run is logged and retried the next day.
- `wristpull-coach-daily.timer` runs `wristpull coach daily` at 07:00, after the pull.
- `wristpull-import.service` is a oneshot for the USB import, started by the udev rule in `../udev/`
  when the watch is plugged in. It needs your desktop session (gio mounts the MTP device), so it is a
  *user* unit; it will not work from the system instance.

Order matters for Garmin watches: import over USB **before** any phone app syncs, because the watch
deletes monitoring files once a sync client has fetched them (see the README).
