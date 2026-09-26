from __future__ import annotations

import json
from functools import lru_cache

from sqlalchemy import Executable
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .config import get_settings

# NOTIFY channel: payload is a vod id whose cached API responses are stale. Sent by
# database triggers on every vods/games write (migration 0006), when the write commits.
VOD_CHANGED = "vods_changed"
# A VOD_CHANGED payload of ROWS_MOVED + id, sent by a merge or split (worker splices.py):
# the VOD's chat rows and emotes moved, so their cached responses are stale too.
ROWS_MOVED = "moved:"


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        json_serializer=_dumps,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def execute(stmt: Executable) -> None:
    """Run one write statement in its own transaction."""
    async with get_sessionmaker()() as s:
        await s.execute(stmt)
        await s.commit()
