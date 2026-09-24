# archive-worker: Twitch monitor, HLS capture, ffmpeg processing, YouTube upload, admin API.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
RUN uv sync --frozen --no-dev --no-install-workspace --package archive-worker

COPY packages/common packages/common
COPY services/worker services/worker
RUN uv sync --frozen --no-dev --no-editable --package archive-worker

ENV PATH=/app/.venv/bin:$PATH ARCHIVE_DATA_DIR=/data
USER 1000:1000
EXPOSE 3031
CMD ["archive-worker", "run"]
