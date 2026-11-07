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
