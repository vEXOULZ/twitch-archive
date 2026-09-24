"""State handed to every job step."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import update

from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import Job, Vod
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix

from .youtube import YouTube


@dataclass
class Deps:
    settings: Settings
    helix: Helix
    gql: Gql
    youtube: YouTube


class StepError(RuntimeError):
    """Expected failure with a readable message (no traceback in job.last_error)."""


@dataclass
class JobContext:
    job_id: int
    kind: str
    vod_id: str | None
    payload: dict[str, Any]
    deps: Deps
    log: logging.LoggerAdapter = field(init=False)

    def __post_init__(self) -> None:
        self.log = logging.LoggerAdapter(
            logging.getLogger("archive_worker.job"), {"job": self.job_id}
        )

    @property
    def settings(self) -> Settings:
        return self.deps.settings

    @property
    def video_type(self) -> str:
        """'vod' (Twitch VOD copy) or 'live' (recording of the live stream)."""
        return self.payload.get("type", "vod")

    def require_vod_id(self) -> str:
        if not self.vod_id:
            raise StepError("job has no vod id yet")
        return self.vod_id

    # ── Paths ─────────────────────────────────────────────────────────────

    @property
    def work_dir(self) -> Path:
        if self.video_type == "live":
            return self.settings.live_dir / str(self.payload["stream_id"])
        return self.settings.vod_dir / self.require_vod_id()

    @property
    def hls_dir(self) -> Path:
        return self.work_dir / "hls"

    @property
    def parts_dir(self) -> Path:
        return self.work_dir / "parts"

    @property
    def default_mp4(self) -> Path:
        name = self.payload["stream_id"] if self.video_type == "live" else self.require_vod_id()
        return self.work_dir / f"{name}.mp4"

    @property
    def source_mp4(self) -> Path:
        return Path(self.payload.get("mp4") or self.default_mp4)

    # ── Persistence ───────────────────────────────────────────────────────

    async def save(self) -> None:
        async with get_sessionmaker()() as s:
            await s.execute(
                update(Job).where(Job.id == self.job_id).values(payload=self.payload, vod_id=self.vod_id)
            )
            await s.commit()

    async def get_vod(self) -> Vod:
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, self.require_vod_id())
            if vod is None:
                raise StepError(f"vod {self.vod_id} not found in database")
            return vod

    async def update_vod(self, **values: Any) -> None:
        async with get_sessionmaker()() as s:
            await s.execute(update(Vod).where(Vod.id == self.require_vod_id()).values(**values))
            await s.commit()
