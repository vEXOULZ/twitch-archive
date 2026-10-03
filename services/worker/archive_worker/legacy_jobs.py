"""The jobs table the worker used before vex-platform's ``JobRuntime`` (``public.jobs``).

Nothing is queued here any more (see jobs.py); ``Runner`` only finishes the jobs that were already
in the table, and the admin API can still resume, pause, retry, cancel and change them. A job is a
list of named steps: ``jobs.step`` is the first step that has not finished, advanced (together with
``jobs.payload``) only after a step returns, so a restarted worker never repeats a finished step.

Gates come from the job's own ``pause_before`` or, when that is NULL, ``Settings.manual_steps[kind]``.
``pause_next`` pauses at the next step boundary once. Gates are checked when a job moves on to a
step, so resuming, retrying or recovering a job runs its current step.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import Any

from archive_common.config import Settings
from archive_common.db import execute, get_sessionmaker
from archive_common.models import Job
from sqlalchemy import func, or_, select, update
from vex_platform.jobs import JobConflict, JobNotFound, Registry

from .context import Deps, JobContext, StepError, StepRefused

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def gates(job: Job, settings: Settings) -> list[str]:
    """Steps this job pauses before: its own override, else the global per-kind setting."""
    if job.pause_before is not None:
        return job.pause_before
    return settings.manual_steps.get(job.kind, [])


async def _load(s, job_id: int) -> Job:  # type: ignore[no-untyped-def]
    job = await s.get(Job, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job  # type: ignore[no-any-return]


async def retry(job_id: int) -> Job:
    """Queue a job again at its current step, with a fresh attempt count."""
    async with get_sessionmaker()() as s:
        job = await _load(s, job_id)
        job.state = "queued"
        job.attempts = 0
        job.not_before = None
        await s.commit()
        return job


async def resume(job_id: int, *, once: bool = False) -> Job:
    """Queue a paused job at its current step; ``once`` pauses it again after that step."""
    async with get_sessionmaker()() as s:
        job = await _load(s, job_id)
        if job.state != "paused":
            raise JobConflict(f"Job is {job.state}; only paused jobs can be resumed")
        job.state = "queued"
        job.pause_next = once
        await s.commit()
        return job


async def pause(job_id: int) -> Job:
    """Pause a queued job now, or a running one when its current step finishes
    (``state`` stays "running" until then). Pausing a paused job is a no-op."""
    async with get_sessionmaker()() as s:
        job = await _load(s, job_id)
        if job.state == "queued":
            job.state = "paused"
        elif job.state == "running":
            job.pause_next = True
        elif job.state != "paused":
            raise JobConflict(f"Job is {job.state}; only queued or running jobs can be paused")
        await s.commit()
        return job


async def set_control(registry: Registry, job_id: int, **values: Any) -> Job:
    """Change a job's ``pause_before`` and/or ``pause_next``; they apply when it next moves on to a step."""
    async with get_sessionmaker()() as s:
        job = await _load(s, job_id)
        if values.get("pause_before") is not None:
            registry.check_steps(job.kind, values["pause_before"])
        for key, value in values.items():
            setattr(job, key, value)
        await s.commit()
        await s.refresh(job)  # updated_at is set by the database
        return job


def _exclusive_key(job: Job) -> str:
    # One job per (vod, video type) at a time; the live recording and the VOD
    # capture of the same stream run side by side, and bot chat beside the archive steps.
    typ = "bot_chat" if job.kind == "bot_chat" else (job.payload or {}).get("type", "vod")
    return f"{job.vod_id}:{typ}" if job.vod_id else f"job:{job.id}"


class Runner:
    """Runs the queued jobs of the table until there are none; the kinds and steps are the registry's."""

    def __init__(self, deps: Deps, registry: Registry, concurrency: int | None = None) -> None:
        self.deps = deps
        self.registry = registry
        self._concurrency = concurrency  # None: Settings.runner_concurrency, read on every pick
        self.running: dict[int, asyncio.Task] = {}  # type: ignore[type-arg]
        self.running_keys: dict[int, str] = {}
        self.cancelling: set[int] = set()
        self.wakeup = asyncio.Event()

    async def resume(self, job_id: int, *, once: bool = False) -> Job:
        job = await resume(job_id, once=once)
        self.poke()
        return job

    async def retry(self, job_id: int) -> Job:
        job = await retry(job_id)
        self.poke()
        return job

    async def cancel(self, job_id: int) -> Job:
        """Cancel a queued or paused job, or stop a running one (at once, mid-step)."""
        async with get_sessionmaker()() as s:
            job = await _load(s, job_id)
            if job.state in ("queued", "paused"):
                job.state = "cancelled"
                await s.commit()
                return job
        task = self.running.get(job_id)
        if job.state == "running" and task is not None:
            self.cancelling.add(job_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with get_sessionmaker()() as s:
                job = await _load(s, job_id)
        if job.state != "cancelled":
            raise JobConflict(f"Job is {job.state}; only queued, paused or running jobs can be cancelled")
        return job

    async def recover(self) -> None:
        """Jobs left 'running' by a previous process are resumed."""
        async with get_sessionmaker()() as s:
            res = await s.execute(update(Job).where(Job.state == "running").values(state="queued"))
            await s.commit()
            if res.rowcount:
                log.info("re-queued %d interrupted job(s)", res.rowcount)

    @property
    def concurrency(self) -> int:
        return self._concurrency or self.deps.settings.runner_concurrency

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
            task.add_done_callback(lambda _t, jid=job.id: self._done(jid))  # type: ignore[misc]

    def _done(self, job_id: int) -> None:
        self.running.pop(job_id, None)
        self.running_keys.pop(job_id, None)
        self.cancelling.discard(job_id)
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
                return job  # type: ignore[no-any-return]
        return None

    async def _set(self, job_id: int, **values: Any) -> None:
        await execute(update(Job).where(Job.id == job_id).values(**values))

    async def _advance(self, job: Job, step: str, ctx: JobContext) -> bool:
        """Checkpoint ``step`` as next; True if the job should pause before it."""
        async with get_sessionmaker()() as s:
            pause_next = (
                await s.execute(
                    update(Job)
                    .where(Job.id == job.id)
                    .values(step=step, payload=ctx.payload, vod_id=ctx.vod_id)
                    .returning(Job.pause_next)
                )
            ).scalar_one()
            pausing = pause_next or step in gates(job, self.deps.settings)
            if pausing:
                await s.execute(update(Job).where(Job.id == job.id).values(state="paused", pause_next=False))
            await s.commit()
            return pausing

    async def _run(self, job: Job) -> None:
        kind = self.registry.kinds.get(job.kind)
        if kind is None:
            await self._set(job.id, state="failed", last_error=f"unknown kind {job.kind}")
            return
        steps = kind.steps
        ctx = JobContext(job.id, job.kind, job.vod_id, dict(job.payload or {}), self.deps)
        start = steps.index(job.step) if job.step in steps else 0
        # The step to resume from. Advanced as soon as a step returns, so the error
        # paths below record progress even if the checkpoint write itself failed.
        current: str | None = steps[start]
        ctx.step = current
        ctx.log.info("running %s (vod=%s) from step %s", job.kind, job.vod_id, current)
        try:
            for i in range(start, len(steps)):
                ctx.step = steps[i]
                ctx.log.info("step %s", steps[i])
                await self.registry.steps[steps[i]](ctx)
                current = steps[i + 1] if i + 1 < len(steps) else None
                if current is not None and await self._advance(job, current, ctx):
                    ctx.step = current
                    ctx.log.info("paused before step %s", current)
                    return
            await self._set(
                job.id,
                state="done",
                step=None,
                payload=ctx.payload,
                vod_id=ctx.vod_id,
                last_error=None,
                not_before=None,
                pause_next=False,
            )
            ctx.step = None
            ctx.log.info("job %s finished", job.kind)
        except asyncio.CancelledError:
            # Worker shutdown re-queues the job; an admin cancel ends it.
            state = "cancelled" if job.id in self.cancelling else "queued"
            ctx.log.info("cancelled" if state == "cancelled" else "interrupted by shutdown; will resume")
            await asyncio.shield(self._set(job.id, state=state, step=current, payload=ctx.payload, vod_id=ctx.vod_id))
            raise
        except Exception as exc:
            # A refused step would be refused again: no retries.
            max_attempts = self.deps.settings.max_attempts
            attempts = max_attempts if isinstance(exc, StepRefused) else job.attempts + 1
            if isinstance(exc, StepError):
                err = str(exc)
            else:
                err = "".join(traceback.format_exception(exc))[-4000:]
            not_before = None
            if attempts >= max_attempts:
                state = "failed"
                ctx.log.error("job failed permanently: %s", exc)
            else:
                state = "queued"
                delay = 60 * 2**attempts
                not_before = _now() + dt.timedelta(seconds=delay)
                ctx.log.warning("job step failed (%s); retry %d in %ds", exc, attempts, delay)
            await self._set(
                job.id,
                state=state,
                step=current,
                attempts=attempts,
                last_error=err,
                payload=ctx.payload,
                vod_id=ctx.vod_id,
                not_before=not_before,
            )

    async def shutdown(self) -> None:
        for task in list(self.running.values()):
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
