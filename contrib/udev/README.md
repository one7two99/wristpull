# contrib/udev

`91-wristpull-garmin-docker.rules` makes the watch's device node readable for group `plugdev`,
hides the watch from the desktop's MTP handling (gvfs, KDE) so nothing else holds it while the
container opens it, and starts `wristpull-import-docker.service` (`../systemd/`) when the watch is
plugged in. `contrib/bin/wristpull-setup` installs it; by hand:

```sh
sudo cp 91-wristpull-garmin-docker.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# plug the watch in, then check:
udevadm info -q property /dev/bus/usb/$(lsusb -d 091e: | sed -E 's/Bus ([0-9]+) Device ([0-9]+).*/\1\/\2/') | grep -E 'ID_MTP|SYSTEMD'
ls -l /dev/bus/usb/<bus>/<dev>        # crw-rw---- root plugdev
```

The import on plug-in needs a running `systemd --user` instance for your user and the unit
installed (`loginctl enable-linger $USER` for imports without a desktop session); without the unit
the rule only sets the permissions and keeps the desktop away from the watch.

With the rule the desktop no longer offers the watch as a drive; that is intended (nothing else may
hold the device while the container opens it). To use the watch on the desktop again, remove the
rule and reload.
