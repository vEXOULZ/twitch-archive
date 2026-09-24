"""Twitch watcher (replaces the legacy src/check.js).

Every ``monitor_interval_seconds``: look up the channel's live stream, keep the
``streams`` row current, and enqueue one ``live`` job (live_record) and one
``archive`` job (vod_download) per stream.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from archive_common.db import get_sessionmaker
from archive_common.models import Stream, Vod
from archive_common.twitch.helix import Helix

from . import jobs

log = logging.getLogger(__name__)


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


async def upsert_vod(video: dict) -> None:
    """Create the vods row for a Helix video, or refresh its title."""
    async with get_sessionmaker()() as s:
        stmt = insert(Vod).values(
            id=video["id"],
            title=video.get("title"),
            created_at=_ts(video.get("created_at")) or dt.datetime.now(dt.timezone.utc),
            stream_id=str(video["stream_id"]) if video.get("stream_id") else None,
            platform="twitch",
        )
        stmt = stmt.on_conflict_do_update(index_elements=[Vod.id], set_={"title": stmt.excluded.title})
        await s.execute(stmt)
        await s.commit()


async def upsert_stream(stream_id: str, started_at: dt.datetime | None, is_live: bool) -> None:
    async with get_sessionmaker()() as s:
        stmt = insert(Stream).values(id=int(stream_id), started_at=started_at, platform="twitch", is_live=is_live)
        stmt = stmt.on_conflict_do_update(index_elements=[Stream.id], set_={"is_live": is_live})
        await s.execute(stmt)
        await s.commit()


async def mark_offline(except_id: str | None = None) -> None:
    async with get_sessionmaker()() as s:
        stmt = update(Stream).where(Stream.is_live.is_(True))
        if except_id:
            stmt = stmt.where(Stream.id != int(except_id))
        await s.execute(stmt.values(is_live=False))
        await s.commit()


class Monitor:
    def __init__(self, helix: Helix, runner: jobs.Runner) -> None:
        self.helix = helix
        self.runner = runner
        self.settings = helix.settings

    async def run_forever(self) -> None:
        if not self.helix.configured:
            log.warning("Twitch client id/secret not set; monitor disabled")
            return
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("monitor tick failed")
            await asyncio.sleep(self.settings.monitor_interval_seconds)

    async def tick(self) -> None:
        s = self.settings
        stream = await self.helix.get_stream(s.twitch_id)
        if not stream:
            await mark_offline()
            return
        stream_id = str(stream["id"])
        await upsert_stream(stream_id, _ts(stream.get("started_at")), True)
        await mark_offline(except_id=stream_id)

        if s.live_record and not await jobs.exists_any("live", stream_id=stream_id):
            log.info("stream %s is live; starting live recording", stream_id)
            await jobs.enqueue("live", None, {"type": "live", "stream_id": stream_id, "login": s.twitch_username})
            self.runner.poke()

        if s.vod_download and not await jobs.exists_any("archive", stream_id=stream_id):
            video = next(
                (v for v in await self.helix.list_videos(s.twitch_id) if str(v.get("stream_id")) == stream_id),
                None,
            )
            if video is None:
                log.info("stream %s has no VOD yet", stream_id)
                return
            await upsert_vod(video)
            log.info("stream %s -> vod %s; starting archive job", stream_id, video["id"])
            await jobs.enqueue("archive", video["id"], {"type": "vod", "stream_id": stream_id})
            self.runner.poke()
