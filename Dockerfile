# wristpull: one image for the web dashboard (service `web`) and every other command (service `cli`).
# Build: docker compose build     Dashboard: docker compose up -d     Commands: docker compose run --rm cli wristpull ...
# See README "Running everything in Docker" and docs/docker-concept.md.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv

# adb: the phone over USB or Wi-Fi (the adb server runs in the `cli` container, keys under $HOME/.android).
# mtp-tools: the watch over USB/MTP (libmtp command-line tools behind src/wristpull/mtp.py).
# bleak (extra `ble`) talks to the strap through the host's BlueZ over D-Bus (compose mounts the socket into `cli`).
# nano (fallback) and micro: editors for `workout new --edit` and the TUI ($EDITOR comes from the host shell).
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends adb mtp-tools nano micro >/dev/null \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/wristpull \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    WRISTPULL_IN_CONTAINER=1

WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra web --extra coach --extra tui --extra ble --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra web --extra coach --extra tui --extra ble --no-editable \
    && rm -rf /app/src /root/.cache

# The container runs as the host user (compose: user: "${UID}:${GID}") so files in the data
# directory keep their owner; this fallback user is only used when no user is given.
# $HOME is world-writable because the runtime UID has no passwd entry; the `cli` service mounts
# the adb keys at $HOME/.android.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin wristpull \
    && mkdir -p /home/wristpull && chmod 1777 /home/wristpull
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# Exactly ONE uvicorn worker: the login rate limit and the job registry live in process memory.
# `wristpull serve` hard-wires workers=1; never replace this with a multi-worker command.
CMD ["wristpull", "--config", "/config/config.toml", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data", "--behind-proxy"]
