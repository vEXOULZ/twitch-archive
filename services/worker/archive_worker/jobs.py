"""Postgres-backed job queue and runner.

A job is a list of named steps (see ``KINDS``). ``jobs.step`` is the first step
that has not finished: the runner advances it (together with ``jobs.payload``,
which carries state between steps) only after a step returns, so a restarted
worker never repeats a finished step. A step interrupted part-way is re-run, so
steps must tolerate that (most checkpoint their own progress in the payload).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import Any

from sqlalchemy import Select, func, or_, select, update

from archive_common.db import execute, get_sessionmaker
from archive_common.models import Job

from .context import Deps, JobContext, StepError
from .steps import STEPS

log = logging.getLogger(__name__)

KINDS: dict[str, list[str]] = {
    # Stream went live (or /admin/hls/download): follow the VOD playlist, then process.
    "archive": ["capture", "finalize", "chapters", "chat", "emotes", "split", "upload", "describe", "cleanup"],
    # /admin/download: full VOD (or a given file), split + upload, optional part range.
    "download": ["ensure_source", "chapters", "split", "upload", "describe", "cleanup"],
    "reupload": ["ensure_source", "split", "upload", "describe", "cleanup"],
    # Recording of the live stream itself (unmuted), uploaded as type "live".
    "live": ["live_record", "resolve_vod", "finalize", "chapters", "split", "upload", "describe", "cleanup"],
    # /v2/live callback from an external recorder.
    "live_file": ["ensure_source", "chapters", "split", "upload", "describe"],
    "dmca": ["ensure_source", "dmca_edit", "split", "upload", "describe", "cleanup"],
    "part_dmca": ["ensure_source", "split", "dmca_edit", "upload", "describe", "cleanup"],
    "chat": ["chat"],
    "logs_manual": ["logs_manual"],
    "chapters": ["chapters"],
    "emotes": ["emotes"],
    "describe": ["describe"],
}

MAX_ATTEMPTS = 3
ACTIVE = ("queued", "running")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def enqueue(kind: str, vod_id: str | None, payload: dict[str, Any] | None = None) -> Job:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind}")
    async with get_sessionmaker()() as s:
        job = Job(kind=kind, vod_id=vod_id, state="queued", step=KINDS[kind][0], payload=payload or {})
        s.add(job)
        await s.commit()
        await s.refresh(job)
    log.info("enqueued job %s %s vod=%s", job.id, kind, vod_id)
    return job


def _matching(stmt: Select, kind: str, vod_id: str | None, stream_id: str | None) -> Select:
    stmt = stmt.where(Job.kind == kind)
    if vod_id is not None:
        stmt = stmt.where(Job.vod_id == vod_id)
    if stream_id is not None:
        stmt = stmt.where(Job.payload["stream_id"].astext == str(stream_id))
    return stmt.limit(1)


async def find_active(kind: str, *, vod_id: str | None = None, stream_id: str | None = None) -> Job | None:
    stmt = _matching(select(Job).where(Job.state.in_(ACTIVE)), kind, vod_id, stream_id)
    async with get_sessionmaker()() as s:
        return (await s.execute(stmt)).scalar_one_or_none()


async def exists_any(kind: str, *, vod_id: str | None = None, stream_id: str | None = None) -> bool:
    """Any job (including finished/failed) — used so the monitor enqueues once per stream."""
    async with get_sessionmaker()() as s:
        return (await s.execute(_matching(select(Job.id), kind, vod_id, stream_id))).first() is not None


async def retry(job_id: int) -> Job | None:
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        if job is None:
            return None
        job.state = "queued"
        job.attempts = 0
        job.not_before = None
        await s.commit()
        return job


async def cancel(job_id: int) -> Job | None:
    async with get_sessionmaker()() as s:
        job = await s.get(Job, job_id)
        if job is None:
            return None
        if job.state == "queued":
            job.state = "cancelled"
        await s.commit()
        return job


def _exclusive_key(job: Job) -> str:
    # One job per (vod, video type) at a time; the live recording and the VOD
    # capture of the same stream run side by side.
    typ = (job.payload or {}).get("type", "vod")
    return f"{job.vod_id}:{typ}" if job.vod_id else f"job:{job.id}"


class Runner:
    def __init__(self, deps: Deps, concurrency: int = 3) -> None:
        self.deps = deps
        self.concurrency = concurrency
        self.running: dict[int, asyncio.Task] = {}
        self.running_keys: dict[int, str] = {}
        self.wakeup = asyncio.Event()

    async def recover(self) -> None:
        """Jobs left 'running' by a previous process are resumed."""
        async with get_sessionmaker()() as s:
            res = await s.execute(update(Job).where(Job.state == "running").values(state="queued"))
            await s.commit()
            if res.rowcount:
                log.info("re-queued %d interrupted job(s)", res.rowcount)

    def poke(self) -> None:
        self.wakeup.set()

    async def run_forever(self) -> None:
        await self.recover()
        while True:
            try:
                await self._fill()
            except Exception:
                log.exception("job runner loop error")
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=10)
            except TimeoutError:
                pass
            self.wakeup.clear()

    async def _fill(self) -> None:
        while len(self.running) < self.concurrency:
            job = await self._claim()
            if job is None:
                return
            task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            self.running[job.id] = task
            self.running_keys[job.id] = _exclusive_key(job)
            task.add_done_callback(lambda _t, jid=job.id: self._done(jid))

    def _done(self, job_id: int) -> None:
        self.running.pop(job_id, None)
        self.running_keys.pop(job_id, None)
        self.poke()

    async def _claim(self) -> Job | None:
        busy = set(self.running_keys.values())
        async with get_sessionmaker()() as s:
            stmt = (
                select(Job)
                .where(Job.state == "queued", or_(Job.not_before.is_(None), Job.not_before <= func.now()))
                .order_by(Job.id)
                .with_for_update(skip_locked=True)
                .limit(50)
            )
            for job in (await s.execute(stmt)).scalars():
                if _exclusive_key(job) in busy:
                    continue
                job.state = "running"
                await s.commit()
                return job
        return None

    async def _set(self, job_id: int, **values: Any) -> None:
        await execute(update(Job).where(Job.id == job_id).values(**values))

    async def _run(self, job: Job) -> None:
        steps = KINDS.get(job.kind)
        if steps is None:
            await self._set(job.id, state="failed", last_error=f"unknown kind {job.kind}")
            return
        ctx = JobContext(job.id, job.kind, job.vod_id, dict(job.payload or {}), self.deps)
        start = steps.index(job.step) if job.step in steps else 0
        # The step to resume from. Advanced as soon as a step returns, so the error
        # paths below record progress even if the checkpoint write itself failed.
        current: str | None = steps[start]
        ctx.log.info("running %s (vod=%s) from step %s", job.kind, job.vod_id, current)
        try:
            for i in range(start, len(steps)):
                ctx.log.info("step %s", steps[i])
                await STEPS[steps[i]](ctx)
                current = steps[i + 1] if i + 1 < len(steps) else None
                if current is not None:
                    await self._set(job.id, step=current, payload=ctx.payload, vod_id=ctx.vod_id)
            await self._set(job.id, state="done", step=None, payload=ctx.payload, vod_id=ctx.vod_id,
                            last_error=None, not_before=None)
            ctx.log.info("job %s finished", job.kind)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._set(job.id, state="queued", step=current, payload=ctx.payload, vod_id=ctx.vod_id)
            )
            raise
        except Exception as exc:
            attempts = job.attempts + 1
            if isinstance(exc, StepError):
                err = str(exc)
            else:
                err = "".join(traceback.format_exception(exc))[-4000:]
            not_before = None
            if attempts >= MAX_ATTEMPTS:
                state = "failed"
                ctx.log.error("job failed permanently: %s", exc)
            else:
                state = "queued"
                delay = 60 * 2**attempts
                not_before = _now() + dt.timedelta(seconds=delay)
                ctx.log.warning("job step failed (%s); retry %d in %ds", exc, attempts, delay)
            await self._set(job.id, state=state, step=current, attempts=attempts, last_error=err,
                            payload=ctx.payload, vod_id=ctx.vod_id, not_before=not_before)

    async def shutdown(self) -> None:
        for task in list(self.running.values()):
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
