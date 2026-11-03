# wristpull web dashboard. Build: docker compose build   Run: docker compose up -d   (see README "Web dashboard")
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra web --extra coach --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra web --extra coach --no-editable \
    && rm -rf /app/src /root/.cache

# The container runs as the host user (compose: user: "${UID}:${GID}") so files in the data
# directory keep their owner; this fallback user is only used when no user is given.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin wristpull
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# Exactly ONE uvicorn worker: the login rate limit and the job registry live in process memory.
# `wristpull serve` hard-wires workers=1; never replace this with a multi-worker command.
CMD ["wristpull", "--config", "/config/config.toml", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data", "--behind-proxy"]
