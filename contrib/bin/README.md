# contrib/bin

`wristpull` – a shell wrapper that runs the CLI from the Docker image:

```sh
cp contrib/bin/wristpull ~/.local/bin/ && chmod +x ~/.local/bin/wristpull
wristpull status                 # = docker compose run --rm cli wristpull status
wristpull tui
```

It uses `~/dev/wristpull/compose.yaml` unless `WRISTPULL_COMPOSE` points elsewhere, and drops the
pseudo-terminal (`run -T`) when stdin or stdout is not a terminal (timers, pipes). The `.env` next
to the compose file provides UID/GID, the data directory and the config file. See README
"Running everything in Docker".

`wristpull-morning` – the morning routine as a guided script (no timers, nothing runs unattended):

```sh
cp contrib/bin/wristpull-morning ~/.local/bin/ && chmod +x ~/.local/bin/wristpull-morning
wristpull-morning              # 1 watch: plug in → import (+ optional workout push) → unplug
                               # 2 phone: sync watch and strap in Gadgetbridge → plug in → pull phone
                               # 3 coach: optional daily check
wristpull-morning phone coach  # only some steps
```

It waits for the devices with `lsusb` (`WRISTPULL_WATCH_VENDOR=091e`, `WRISTPULL_PHONE_VENDOR=3310`
for a Mudita Kompakt; look yours up with `lsusb`), waits for a running plug-in import service before
touching the watch, and runs everything through the Docker `cli` service (`WRISTPULL_CMD="uv run
wristpull"` switches to the native installation).
