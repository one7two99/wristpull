# contrib/udev

Two variants, install **one** of them:

| File | For | What it does |
|---|---|---|
| `90-wristpull-garmin.rules` | native installation (uv, gvfs mount) | starts `wristpull-import.service` when the watch is plugged in |
| `91-wristpull-garmin-docker.rules` | Docker `cli` service | makes the device node readable for group `plugdev`, hides the watch from the desktop's MTP handling (gvfs, KDE), starts `wristpull-import-docker.service` |

```sh
sudo cp 91-wristpull-garmin-docker.rules /etc/udev/rules.d/     # or 90-wristpull-garmin.rules
sudo udevadm control --reload-rules
# plug the watch in, then check:
udevadm info -q property /dev/bus/usb/$(lsusb -d 091e: | sed -E 's/Bus ([0-9]+) Device ([0-9]+).*/\1\/\2/') | grep -E 'ID_MTP|SYSTEMD'
ls -l /dev/bus/usb/<bus>/<dev>        # crw-rw---- root plugdev
```

Both need a running `systemd --user` instance for your user (`loginctl enable-linger $USER` for
timers and imports without a desktop session). The native rule relies on the user's gvfs/MTP
mounts; on KDE, `kiod6` may grab the device first (`kquitapp6 kiod6 && gio mount mtp://...`).

With the Docker rule the desktop no longer offers the watch as a drive; that is intended (nothing
else may hold the device while the container opens it). To use the native gvfs path on the same
machine again, set `[garmin] access = "gvfs"` and mount by hand, or switch back to the 90 rule.
