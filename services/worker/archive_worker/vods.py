"""vods / streams rows shared by the monitor, admin API and job steps."""

from __future__ import annotations

import datetime as dt
from typing import Any

from archive_common.db import ROWS_MOVED, VOD_CHANGED, get_sessionmaker
from archive_common.models import Stream, Vod, VodSplice
from archive_common.timeutil import format_hhmmss, parse_helix_duration, parse_ts
from sqlalchemy import Select, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_vod(video: dict[str, Any]) -> None:
    """Create the vods row for a Helix video, or refresh its title. Duration and
    thumbnail are only set on insert: capture, finalize and uploads own them after."""
    async with get_sessionmaker()() as s:
        stmt = insert(Vod).values(
            id=video["id"],
            title=video.get("title"),
            created_at=parse_ts(video.get("created_at")) or dt.datetime.now(dt.UTC),
            stream_id=str(video["stream_id"]) if video.get("stream_id") else None,
            duration=format_hhmmss(parse_helix_duration(video.get("duration", ""))),
            thumbnail_url=video.get("thumbnail_url") or None,  # "" while the stream is live
            platform="twitch",
        )
        stmt = stmt.on_conflict_do_update(index_elements=[Vod.id], set_={"title": stmt.excluded.title})
        await s.execute(stmt)
        await s.commit()


def active_splices(*vod_ids: str) -> Select:  # type: ignore[type-arg]
    """Splices (merges, splits) touching any of ``vod_ids`` that are not undone, oldest first."""
    return (
        select(VodSplice)
        .where(VodSplice.undone_at.is_(None), or_(VodSplice.vod_id.in_(vod_ids), VodSplice.other_id.in_(vod_ids)))
        .order_by(VodSplice.id)
    )


async def splice_reason(vod_id: str) -> str | None:
    """Why ``vod_id`` does not match Twitch's VOD of that id (a synthetic VOD, or merged or split), or None.
    The real VODs behind a synthetic one are untouched, so they are never refused."""
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(Vod.merged_into, Vod.synthetic).where(Vod.id == vod_id))).one_or_none()
        merged_into, synthetic = row if row else (None, None)
        if synthetic is not None:
            return f"vod {vod_id} is a synthetic VOD, made of parts of others (run jobs on those)"
        if merged_into:
            return f"vod {vod_id} was merged into {merged_into.get('id')}"
        splice = (
            await s.execute(active_splices(vod_id).order_by(None).order_by(VodSplice.id.desc()).limit(1))
        ).scalar_one_or_none()
    if splice is None:
        return None
    if splice.kind == "merge":
        return f"vod {vod_id} was merged with {splice.other_id if splice.vod_id == vod_id else splice.vod_id}"
    return f"vod {vod_id} was split ({splice.vod_id} | {splice.other_id})"


async def vod_id_for_stream(stream_id: str) -> str | None:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Vod.id).where(Vod.stream_id == stream_id).limit(1))).scalar_one_or_none()  # type: ignore[no-any-return]


async def live_stream_ids() -> list[str]:
    async with get_sessionmaker()() as s:
        return [str(i) for i in (await s.execute(select(Stream.id).where(Stream.is_live.is_(True)))).scalars()]


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


_BOT_LOGS_OUT_OF_ORDER = text("""
    SELECT count(*) FROM (
        SELECT seq, lag(seq) OVER (ORDER BY content_offset_seconds, at, id) AS prev
        FROM bot_logs WHERE vod_id = :vod_id
    ) x WHERE seq < prev
""")
# Fresh sequence values, handed out in (offset, at, id) order: nothing else can take them,
# so a concurrent insert never collides with one, and the gaps left behind are harmless.
_BOT_LOGS_RESEQUENCE = text("""
    WITH o AS (
        SELECT id, row_number() OVER (ORDER BY content_offset_seconds, at, id) AS rn
        FROM bot_logs WHERE vod_id = :vod_id
    ), n AS (
        SELECT v, row_number() OVER (ORDER BY v) AS rn
        FROM (SELECT nextval('bot_logs_seq_seq') AS v FROM generate_series(1, (SELECT count(*) FROM o))) s
    )
    UPDATE bot_logs b SET seq = n.v FROM o JOIN n USING (rn) WHERE b.id = o.id
""")


async def resequence_bot_logs(s: AsyncSession, *vod_ids: str) -> list[str]:
    """Make ``bot_logs.seq`` rise with the offset again for these VODs (the comments API
    pages by it, like ``logs._id``). Returns the VODs it had to renumber."""
    changed = []
    for vod_id in vod_ids:
        if (await s.execute(_BOT_LOGS_OUT_OF_ORDER, {"vod_id": vod_id})).scalar_one():
            await s.execute(_BOT_LOGS_RESEQUENCE, {"vod_id": vod_id})
            changed.append(vod_id)
    return changed


async def notify_rows_moved(s: AsyncSession, *vod_ids: str) -> None:
    """Tell archive-api (on commit) to drop these VODs' cached chat and emotes.

    The vods/games triggers (migration 0006) don't cover the logs and emotes tables.
    """
    for vod_id in vod_ids:
        await s.execute(select(func.pg_notify(VOD_CHANGED, ROWS_MOVED + vod_id)))
