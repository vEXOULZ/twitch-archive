"""vods / streams rows shared by the monitor, admin API and job steps."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from archive_common.db import get_sessionmaker
from archive_common.models import Stream, Vod
from archive_common.timeutil import format_hhmmss, parse_helix_duration, parse_ts


async def upsert_vod(video: dict) -> None:
    """Create the vods row for a Helix video, or refresh its title. Duration and
    thumbnail are only set on insert: capture, finalize and uploads own them after."""
    async with get_sessionmaker()() as s:
        stmt = insert(Vod).values(
            id=video["id"],
            title=video.get("title"),
            created_at=parse_ts(video.get("created_at")) or dt.datetime.now(dt.timezone.utc),
            stream_id=str(video["stream_id"]) if video.get("stream_id") else None,
            duration=format_hhmmss(parse_helix_duration(video.get("duration", ""))),
            thumbnail_url=video.get("thumbnail_url") or None,  # "" while the stream is live
            platform="twitch",
        )
        stmt = stmt.on_conflict_do_update(index_elements=[Vod.id], set_={"title": stmt.excluded.title})
        await s.execute(stmt)
        await s.commit()


async def vod_id_for_stream(stream_id: str) -> str | None:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Vod.id).where(Vod.stream_id == stream_id).limit(1))).scalar_one_or_none()


async def set_live_stream(stream_id: str | None, started_at: dt.datetime | None = None) -> None:
    """Mark ``stream_id`` (upserted) as the only live stream, or none when None."""
    async with get_sessionmaker()() as s:
        offline = update(Stream).where(Stream.is_live.is_(True))
        if stream_id:
            stmt = insert(Stream).values(id=int(stream_id), started_at=started_at, platform="twitch", is_live=True)
            await s.execute(stmt.on_conflict_do_update(index_elements=[Stream.id], set_={"is_live": True}))
            offline = offline.where(Stream.id != int(stream_id))
        await s.execute(offline.values(is_live=False))
        await s.commit()
