from __future__ import annotations

import json
from functools import lru_cache

from sqlalchemy import Executable, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

# NOTIFY channel: payload is a vod id whose cached API responses are stale.
VOD_CHANGED = "vods_changed"


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


async def notify_vod_changed(session: AsyncSession, vod_id: str) -> None:
    """Tell archive-api (LISTENing on ``VOD_CHANGED``) to drop its cached copies of the VOD.
    Delivered when ``session`` commits, so readers never see the old row after the event."""
    await session.execute(text("select pg_notify(:channel, :vod_id)"), {"channel": VOD_CHANGED, "vod_id": vod_id})
