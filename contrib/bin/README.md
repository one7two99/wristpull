# contrib/bin

`wristpull` – a shell wrapper that runs the CLI from the Docker image:

```sh
contrib/bin/wristpull status     # = docker compose run --rm cli wristpull status
contrib/bin/wristpull tui
# optional, to have it on the PATH: cp contrib/bin/wristpull ~/.local/bin/
```

It finds the `compose.yaml` of its checkout by itself (`WRISTPULL_COMPOSE` overrides), and drops the
pseudo-terminal (`run -T`) when stdin or stdout is not a terminal (timers, pipes). The `.env` next
to the compose file provides UID/GID, the data directory and the config file. See README
"Running everything in Docker".

`wristpull-morning` – the morning routine as a guided script (no timers, nothing runs unattended):

```sh
contrib/bin/wristpull-morning              # 1 watch: plug in → import (+ optional workout push) → unplug
                                           # 2 strap: pull the band over Bluetooth (in range, not on the phone)
                                           # 3 coach: optional daily check
contrib/bin/wristpull-morning strap coach  # only some steps
contrib/bin/wristpull-morning watch phone  # the Gadgetbridge way: sync in the app, plug the phone in, pull phone
```

It waits for the devices with `lsusb` (`WRISTPULL_WATCH_VENDOR=091e`, `WRISTPULL_PHONE_VENDOR=3310`
for a Mudita Kompakt; look yours up with `lsusb`), waits for a running plug-in import service before
touching the watch, and runs everything through the Docker `cli` service (`WRISTPULL_CMD="uv run
wristpull"` switches to the native installation).
