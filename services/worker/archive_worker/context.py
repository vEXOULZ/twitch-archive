"""State handed to every job step."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archive_common.config import Settings
from archive_common.db import execute, get_sessionmaker
from archive_common.models import Job, Vod
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix
from sqlalchemy import update
from vex_platform.jobs import StepContext, StepError, StepRefused

from .doomtp import Doomtp
from .events import JOB_LOGGER, UNITS, JobEvents
from .job_rows import subject_of, vod_of
from .vods import splice_reason
from .youtube import YouTube


@dataclass
class Deps:
    settings: Settings
    helix: Helix
    gql: Gql
    youtube: YouTube
    events: JobEvents = field(default_factory=JobEvents)  # the job event log (GET /admin/jobs/{id}/events)
    doomtp: Doomtp = field(init=False)  # doomtp-bot's chat log

    def __post_init__(self) -> None:
        self.doomtp = Doomtp(self.settings)


# StepError: an expected failure with a readable message (no traceback in last_error).
# StepRefused: a step that must not run on this VOD; the job fails at once instead of retrying.
__all__ = ["Deps", "JobContext", "StepError", "StepRefused"]


class JobContext:
    """What a step receives. Under the runtime (``of_run``) it wraps vex-platform's ``StepContext``:
    payload, subject (``vod_id``), step, log, progress and save are the run's. Built directly, it is a
    job of the legacy table (legacy_jobs.py) or a test's."""

    def __init__(
        self,
        job_id: int,
        kind: str,
        vod_id: str | None,
        payload: dict[str, Any],
        deps: Deps,
        *,
        run: StepContext | None = None,
    ) -> None:
        self.job_id = job_id
        self.kind = kind
        self.deps = deps
        self.run = run
        # The worker's settings as the job started or resumed: a dashboard change applies to the next job,
        # not halfway through this one (runtime_settings.py).
        self.settings: Settings = deps.settings.model_copy(deep=True)
        if run is None:
            self._payload = payload
            self._vod_id = vod_id
            # The runner updates "step" as the job moves on, so every line records where it came from.
            self._log = logging.LoggerAdapter(logging.getLogger(JOB_LOGGER), {"job": job_id, "step": None})

    @classmethod
    def of_run(cls, run: StepContext, deps: Deps) -> JobContext:
        return cls(run.run_id, run.kind.name, None, run.payload, deps, run=run)

    @property
    def payload(self) -> dict[str, Any]:
        return self.run.payload if self.run else self._payload

    @payload.setter
    def payload(self, value: dict[str, Any]) -> None:
        if self.run:
            self.run.payload = value
        else:
            self._payload = value

    @property
    def vod_id(self) -> str | None:
        return vod_of(self.run.subject) if self.run else self._vod_id

    @vod_id.setter
    def vod_id(self, value: str | None) -> None:
        if self.run:
            self.run.subject = subject_of(value)
        else:
            self._vod_id = value

    @property
    def log(self) -> Any:
        """``info``, ``warning`` and ``error`` with %-style arguments, like a stdlib logger."""
        return self.run.log if self.run else self._log

    @property
    def step(self) -> str | None:
        return self.run.step if self.run else self._log.extra["step"]  # type: ignore[index, return-value]

    @step.setter
    def step(self, name: str | None) -> None:
        if self.run:
            self.run.step = name
        else:
            self._log.extra["step"] = name  # type: ignore[index]

    def progress(self, done: float, total: float, unit: str, message: str) -> None:
        """Record how far the current step is, for the dashboard (not the process log).
        Safe to call from a worker thread."""
        assert unit in UNITS, unit
        if self.run:
            self.run.progress(done, total, unit, message)
        else:
            self.deps.events.add(self.job_id, "info", self.step, message, {"done": done, "total": total, "unit": unit})

    @property
    def video_type(self) -> str:
        """'vod' (Twitch VOD copy) or 'live' (recording of the live stream)."""
        return self.payload.get("type", "vod")  # type: ignore[no-any-return]

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
        """Checkpoint ``payload`` and ``vod_id`` now, mid-step."""
        if self.run:
            await self.run.save()
        else:
            await execute(update(Job).where(Job.id == self.job_id).values(payload=self.payload, vod_id=self.vod_id))

    async def enqueue(
        self, kind: str, vod_id: str | None, payload: dict[str, Any] | None = None, **options: Any
    ) -> int:
        """Queue a child run of this one (GET /jobs/{id}/related shows them together); its id."""
        if self.run is None:
            raise StepError("only a runtime job can queue other jobs")
        return (await self.run.enqueue(kind, subject_of(vod_id), payload, **options)).run.id  # type: ignore[no-any-return]

    async def get_vod(self) -> Vod:
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, self.require_vod_id())
            if vod is None:
                raise StepError(f"vod {self.vod_id} not found in database")
            return vod  # type: ignore[no-any-return]

    async def update_vod(self, **values: Any) -> None:
        await execute(update(Vod).where(Vod.id == self.require_vod_id()).values(**values))

    async def refuse_if_spliced(self) -> None:
        """For steps that fetch from Twitch by VOD id: the row of a merged or split VOD no
        longer matches Twitch's VOD, and a refresh would overwrite (or add to) it."""
        reason = await splice_reason(self.vod_id) if self.vod_id else None
        if reason:
            raise StepRefused(f"{reason}; step {self.step} refetches it from Twitch, so it is refused")
