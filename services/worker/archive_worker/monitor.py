"""Twitch watcher (replaces the legacy src/check.js).

Every ``monitor_interval_seconds``: look up the channel's live stream, keep the
``streams`` row current, and enqueue one ``live`` job (live_record) and one
``archive`` job (vod_download) per stream.
"""

from __future__ import annotations

import asyncio
import logging

from archive_common.timeutil import parse_ts
from archive_common.twitch.helix import Helix

from . import jobs
from .vods import set_live_stream, upsert_vod

log = logging.getLogger(__name__)


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
            await set_live_stream(None)
            return
        stream_id = str(stream["id"])
        await set_live_stream(stream_id, parse_ts(stream.get("started_at")))

        if s.live_record and not await jobs.exists_any("live", stream_id=stream_id):
            log.info("stream %s is live; starting live recording", stream_id)
            payload = {"type": "live", "stream_id": stream_id, "login": s.twitch_username}
            await self.runner.enqueue("live", None, payload)

        if s.vod_download and not await jobs.exists_any("archive", stream_id=stream_id):
            video = await self.helix.video_for_stream(s.twitch_id, stream_id)
            if video is None:
                log.info("stream %s has no VOD yet", stream_id)
                return
            await upsert_vod(video)
            log.info("stream %s -> vod %s; starting archive job", stream_id, video["id"])
            await self.runner.enqueue("archive", video["id"], {"type": "vod", "stream_id": stream_id})
