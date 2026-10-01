"""audit_log: the copy of admin_audit, and who GET /admin/audit shows (needs the dev DB)."""

import datetime as dt

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select
from vex_platform.actor import SYSTEM
from vex_platform.audit import AuditEntry

from archive_common import audit
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import AdminAudit
from archive_worker import jobs
from archive_worker.admin import create_admin_app

AT = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


@pytest.fixture
async def marks(db):
    """The last ids of both tables; rows after them are removed afterwards."""
    async with get_sessionmaker()() as s:
        old = (await s.execute(select(func.max(AdminAudit.id)))).scalar() or 0
        new = (await s.execute(select(func.max(AUDIT_LOG.c.id)))).scalar() or 0
    yield old, new
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.id > new))
        await s.execute(delete(AdminAudit).where(AdminAudit.id > old))
        await s.commit()


async def _copies(after: int) -> dict[str, dict]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.id > after))).mappings().all()
    return {r["request_id"]: dict(r) for r in rows}


async def test_copy_names_the_routes_and_skips_rows_already_copied(marks):
    old, new = marks
    async with get_sessionmaker()() as s:
        ids = (await s.execute(insert(AdminAudit).returning(AdminAudit.id), [
            {"at": AT, "actor": "twitch:100", "actor_login": "alice", "action": "PATCH /admin/vods/{vod_id}",
             "target": "vod:1", "detail": {"before": {"title": "a"}, "after": {"title": "b"}}},
            {"at": AT, "actor": "api-key", "action": "POST /admin/download", "target": "vod:1",
             "detail": {"vodId": "1", "type": "vod"}},
            {"at": AT, "actor": "password", "action": "POST /admin/gone", "detail": None},
        ])).scalars().all()
        await s.commit()

    assert await audit.copy_admin_audit() >= 3  # the dev DB may hold other uncopied rows
    assert await audit.copy_admin_audit() == 0
    rows = await _copies(new)
    edit, download, gone = (rows[f"admin_audit:{i}"] for i in ids)
    assert (edit["action"], edit["actor_kind"], edit["actor_id"], edit["actor_login"], edit["via"]) == (
        "vod.update", "user", "100", "alice", "web")
    assert (edit["before"], edit["after"], edit["detail"], edit["at"]) == ({"title": "a"}, {"title": "b"}, None, AT)
    assert (download["action"], download["actor_kind"], download["via"], download["detail"]) == (
        "vod.download", "api_key", "api", {"vodId": "1", "type": "vod"})
    assert (gone["action"], gone["actor_id"], gone["detail"]) == (
        "admin.request", "password", {"route": "POST /admin/gone", "detail": None})


async def test_admin_audit_shows_admins_in_the_old_shape(marks, deps):
    deps.settings.admin_api_key = SecretStr("k")
    await audit.write(AuditEntry("vod.update", audit.actor_of("twitch:100", "alice"), "vod:1",
                                 before={"title": "a"}, after={"title": "b"}))
    await audit.write(AuditEntry("job.enqueue", SYSTEM, "job:1"))  # the monitor: not an admin
    app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        rows = (await c.get("/admin/audit?limit=5", headers={"Authorization": "Bearer k"})).json()["data"]
    newest = [r for r in rows if r["id"] > marks[1]]
    assert [(r["actor"], r["actorLogin"], r["action"], r["target"], r["detail"]) for r in newest] == [
        ("twitch:100", "alice", "vod.update", "vod:1", {"before": {"title": "a"}, "after": {"title": "b"}}),
    ]
