"""Jobs as the admin API and the other readers see them: vex-platform's ``jobs.job_runs`` and the
rows left in the legacy ``jobs`` table (legacy_jobs.py), read as one table.

A run's ``subject`` is ``vod:<id>``, read as ``vod_id``, and its ``succeeded`` state as ``done``,
so a row looks the same whichever table it came from. ``legacy`` says which one that was. Ids do
not collide: Alembic 0013 started ``job_runs`` above the legacy ids.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, MetaData, Table, Text, case, func, literal, select, union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from archive_common.models import Job

ACTIVE = ("queued", "running", "paused")
# The v1 admin API's names; job_runs says "succeeded" for "done".
STATES = ("queued", "running", "paused", "done", "failed", "cancelled")

RUNS = Table(
    "job_runs",
    MetaData(schema="jobs"),
    Column("id", BigInteger, primary_key=True),
    Column("kind", Text),
    Column("subject", Text),
    Column("state", Text),
    Column("step", Text),
    Column("payload", JSONB),
    Column("attempts", Integer),
    Column("last_error", Text),
    Column("not_before", DateTime(timezone=True)),
    Column("pause_before", ARRAY(Text)),
    Column("pause_next", Boolean),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)


def subject_of(vod_id: str | None) -> str | None:
    return f"vod:{vod_id}" if vod_id else None


def vod_of(subject: str | None) -> str | None:
    return subject[4:] if subject and subject.startswith("vod:") else None


def _runs():
    r = RUNS.c
    return select(
        r.id, r.kind,
        case((r.subject.startswith("vod:"), func.substr(r.subject, 5))).label("vod_id"),
        case((r.state == "succeeded", "done"), else_=r.state).label("state"),
        r.step, r.attempts, r.last_error, r.payload, r.not_before, r.pause_before, r.pause_next,
        r.created_at, r.updated_at, literal(False).label("legacy"),
    )


def _legacy():
    j = Job.__table__.c
    return select(
        j.id, j.kind, j.vod_id, j.state, j.step, j.attempts, j.last_error, j.payload, j.not_before,
        j.pause_before, j.pause_next, j.created_at, j.updated_at, literal(True).label("legacy"),
    )


ALL_JOBS = union_all(_runs(), _legacy()).subquery("all_jobs")
