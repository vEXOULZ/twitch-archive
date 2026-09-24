# archive-api: read-only Feathers-compatible API. Also carries Alembic for migrations.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
RUN uv sync --frozen --no-dev --no-install-workspace --package archive-api

COPY packages/common packages/common
COPY services/api services/api
COPY alembic.ini ./
COPY migrations migrations
RUN uv sync --frozen --no-dev --no-editable --package archive-api

ENV PATH=/app/.venv/bin:$PATH
USER 1000:1000
EXPOSE 3030
CMD ["archive-api"]
