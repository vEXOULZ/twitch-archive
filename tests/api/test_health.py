"""/healthz is liveness only; /readyz checks the database and answers 503 without it."""

from __future__ import annotations

import archive_api.main as api_main
import httpx
import pytest
from archive_common.config import Settings
from sqlalchemy.ext.asyncio import create_async_engine

UNREACHABLE = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/archive"


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    """The api with a database that refuses every connection."""
    monkeypatch.setattr(api_main, "get_engine", lambda: create_async_engine(UNREACHABLE))
    app = api_main.create_app(Settings(rate_limit_points=1_000_000, cache_ttl_seconds=0))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


async def test_healthz_answers_without_the_database(down: httpx.AsyncClient) -> None:
    async with down as c:
        r = await c.get("/healthz")
    assert (r.status_code, r.json()) == (200, {"ok": True})


async def test_readyz_is_503_without_the_database(down: httpx.AsyncClient) -> None:
    async with down as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["ok"] is False and r.json()["error"].startswith("database: ")
