"""Per-job event log: every ``ctx.log`` line, step change and progress report.

Events are buffered in memory and written in batches (``run_forever`` flushes
about once a second), so logging never waits on the database. Each job keeps
roughly its newest ``cap`` events; older ones are pruned as new ones arrive.
Recording is thread-safe: upload progress arrives from a worker thread.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import defaultdict, deque
from typing import Any

from sqlalchemy import delete, insert, select

from archive_common.db import get_sessionmaker
from archive_common.models import JobEvent

log = logging.getLogger(__name__)

JOB_LOGGER = "archive_worker.job"  # JobContext.log writes here
MAX_PER_JOB = 1000
PRUNE_EVERY = 100  # events per job between prunes
UNITS = ("parts", "bytes", "percent")


def level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    return "info"


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def event_json(event: JobEvent) -> dict[str, Any]:
    return {
        "seq": event.id,
        "at": _iso(event.at),
        "level": event.level,
        "step": event.step,
        "message": event.message,
        "progress": event.progress,
    }


class _Handler(logging.Handler):
    def __init__(self, events: JobEvents) -> None:
        super().__init__(logging.INFO)
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        job = getattr(record, "job", None)
        if job is None:
            return
        try:
            at = dt.datetime.fromtimestamp(record.created, dt.timezone.utc)
            self.events.add(job, level_name(record.levelno), getattr(record, "step", None), record.getMessage(), at=at)
        except Exception:
            self.handleError(record)


class JobEvents:
    def __init__(self, cap: int = MAX_PER_JOB) -> None:
        self.cap = cap
        self._pending: deque[dict[str, Any]] = deque(maxlen=50_000)  # bounded if the database is down
        self._since_prune: defaultdict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self.handler = _Handler(self)

    def install(self) -> None:
        """Record everything logged through a JobContext, INFO and up even when
        ARCHIVE_LOG_LEVEL is higher (logs.setup keeps stderr at that level)."""
        logger = logging.getLogger(JOB_LOGGER)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)

    def uninstall(self) -> None:
        logging.getLogger(JOB_LOGGER).removeHandler(self.handler)

    def add(
        self,
        job_id: int,
        level: str,
        step: str | None,
        message: str,
        progress: dict[str, Any] | None = None,
        *,
        at: dt.datetime | None = None,
    ) -> None:
        self._pending.append({
            "job_id": job_id,
            "at": at or dt.datetime.now(dt.timezone.utc),
            "level": level,
            "step": step,
            "message": message,
            "progress": progress,
        })

    async def flush(self) -> None:
        async with self._lock:
            rows = []
            while self._pending:
                rows.append(self._pending.popleft())
            if not rows:
                return
            try:
                async with get_sessionmaker()() as s:
                    await s.execute(insert(JobEvent), rows)
                    for row in rows:
                        self._since_prune[row["job_id"]] += 1
                    for job_id in [j for j, n in self._since_prune.items() if n >= PRUNE_EVERY]:
                        await s.execute(self._prune(job_id))
                        del self._since_prune[job_id]
                    await s.commit()
            except Exception as exc:
                # Not through the job logger: that would feed this batch's failure back into it.
                log.warning("dropped %d job event(s): %s", len(rows), exc)

    def _prune(self, job_id: int):
        newest_kept = (
            select(JobEvent.id).where(JobEvent.job_id == job_id)
            .order_by(JobEvent.id.desc()).offset(self.cap - 1).limit(1).scalar_subquery()
        )
        return delete(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id < newest_kept)

    async def run_forever(self, interval: float = 1.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self.flush()
        finally:
            await asyncio.shield(self.flush())

    async def list(self, job_id: int, after: int = 0, limit: int = 200) -> list[JobEvent]:
        await self.flush()  # include what was logged a moment ago
        async with get_sessionmaker()() as s:
            stmt = (
                select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > after)
                .order_by(JobEvent.id).limit(limit)
            )
            return list((await s.execute(stmt)).scalars())
