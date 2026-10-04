# One image per service, built as targets of this file:
#   docker build --target api .      archive-api: read-only Feathers-compatible API; also carries Alembic
#   docker build --target worker .   archive-worker: Twitch monitor, HLS capture, ffmpeg, YouTube upload, admin API

FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app
# Dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/

FROM base AS api-build
RUN uv sync --frozen --no-dev --no-install-workspace --package archive-api
COPY packages/common packages/common
COPY services/api services/api
RUN uv sync --frozen --no-dev --no-editable --package archive-api

FROM base AS worker-build
RUN uv sync --frozen --no-dev --no-install-workspace --package archive-worker
COPY packages/common packages/common
COPY services/worker services/worker
RUN uv sync --frozen --no-dev --no-editable --package archive-worker

FROM python:3.13-slim AS api
WORKDIR /app
COPY --from=api-build /app/.venv .venv
# Migrations run from this image: `docker compose run --rm api alembic upgrade head`.
COPY alembic.ini ./
COPY migrations migrations
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 1000:1000
EXPOSE 3030
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ARCHIVE_API_PORT', '3030'), timeout=5)"]
CMD ["archive-api"]

FROM python:3.13-slim AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp (seek-bar previews backfill) needs a JavaScript runtime for YouTube.
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
WORKDIR /app
COPY --from=worker-build /app/.venv .venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 ARCHIVE_DATA_DIR=/data
USER 1000:1000
EXPOSE 3031
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ARCHIVE_ADMIN_PORT', '3031'), timeout=5)"]
CMD ["archive-worker", "run"]
