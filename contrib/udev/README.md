# udev rule: import when the watch is plugged in

`90-wristpull-garmin.rules` starts the user unit `wristpull-import.service` (see `../systemd/`) when a
Garmin MTP device (vendor id 091e) appears. Install:

```sh
sudo cp 90-wristpull-garmin.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

`SYSTEMD_USER_WANTS` requires a running user session with `systemd --user`; the import runs as you,
with your gvfs/MTP mounts. On KDE, `kiod6` may grab the device first; the import then prints the
`kquitapp6 kiod6 && gio mount …` hint, or you configure KDE not to auto-mount MTP devices.
