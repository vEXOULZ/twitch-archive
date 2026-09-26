"""archive-api drops cached responses when the worker NOTIFYs that a VOD changed."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from archive_api import invalidation
from archive_api.comments import Comments
from archive_api.invalidation import VodInvalidator, asyncpg_dsn
from archive_api.middleware import ResponseCache
from archive_common.config import get_settings
from archive_common.db import VOD_CHANGED, get_engine

KEYS = ["vods/1", "vods/12", "vods?$limit=10", "games?vodId=1", "games/5", "v1/games-played",
        "emotes/1", "emotes/12", "emotes?vodId=1", "streams?"]
LEFT = ["vods/12", "emotes/12", "streams?"]


def _filled(keys=KEYS) -> ResponseCache:
    cache = ResponseCache(300)
    for key in keys:
        cache.set(key, object())
    return cache


def _left(cache: ResponseCache) -> list[str]:
    return [k for k in KEYS if cache.get(k) is not None]


def test_asyncpg_dsn():
    assert asyncpg_dsn("postgresql+asyncpg://u:p%40ss@h:5/db") == "postgresql://u:p%40ss@h:5/db"


def test_invalidate_drops_the_vod_and_everything_listing_vods():
    service, status = _filled(), _filled(["status"])
    VodInvalidator("postgresql+asyncpg://x@h/db", service, status).invalidate("1")
    assert _left(service) == LEFT
    assert status.get("status") is None


def test_invalidate_drops_the_vods_chat_replay():
    """A merge or split re-keys chat rows; the notice for the VOD row drops its cached pages."""
    comments = Comments(ResponseCache(300), ResponseCache(300))
    for key in ("offset:1:0", "offset:12:0"):
        comments.cache.set(key, object())
    for key in ("cursor:1:abc", "start:1", "start:12"):
        comments.long_cache.set(key, object())
    VodInvalidator("postgresql+asyncpg://x@h/db", _filled(), comments=comments).invalidate("1")
    assert [k for k in ("offset:1:0", "offset:12:0") if comments.cache.get(k)] == ["offset:12:0"]
    assert [k for k in ("cursor:1:abc", "start:1", "start:12") if comments.long_cache.get(k)] == ["start:12"]


@pytest.fixture
async def db():
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


async def test_listener_invalidates_on_notify(db, monkeypatch):
    monkeypatch.setattr(invalidation, "RECONNECT_DELAY", 0.05)
    service = _filled()
    listener = VodInvalidator(get_settings().database_url, service)
    cleared = asyncio.Event()
    monkeypatch.setattr(listener, "clear_all", cleared.set)  # (re)connected and listening
    task = asyncio.create_task(listener.run_forever())
    try:
        await asyncio.wait_for(cleared.wait(), 5)
        async with get_engine().connect() as conn:
            await conn.execute(text("select pg_notify(:c, '1')"), {"c": VOD_CHANGED})
            await conn.commit()
        for _ in range(100):
            if service.get("vods/1") is None:
                break
            await asyncio.sleep(0.05)
        assert _left(service) == LEFT
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
