"""/api/v2: auth, problem details, request ids, the job and audit routes, docs (needs the dev DB)."""

import datetime as dt

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, text

from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import CSRF_HEADER, SESSION_COOKIE
from archive_worker.job_rows import RUNS, subject_of

VOD = "test-api-v2-vod"
KEY = {"Authorization": "Bearer k"}


async def _reset():
    async with get_sessionmaker()() as s:
        runs = select(text("'job:' || id")).select_from(RUNS).where(RUNS.c.subject == subject_of(VOD))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.in_(runs)))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.action == "request.denied",
                                                text("detail->>'path' LIKE '/api/v2/%'")))
        await s.execute(delete(RUNS).where(RUNS.c.subject == subject_of(VOD)))
        await s.execute(text("SET LOCAL search_path TO jobs, public"))
        await s.execute(text("DELETE FROM procrastinate_jobs WHERE lock LIKE :vod"), {"vod": f"vod:{VOD}:%"})
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


@pytest.fixture
async def vod(db):
    await _reset()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=VOD, title="t", created_at=dt.datetime.now(dt.timezone.utc), duration="00:10:00"))
        await s.commit()
    yield VOD
    await _reset()


@pytest.fixture
async def admin(vod, deps, make_service, monkeypatch):
    """An admin app whose runtime knows kind "test" (steps a, b) and "refetch" (a TWITCH_STEPS step);
    nothing runs them."""
    monkeypatch.setitem(jobs.KINDS, "test", ["a", "b"])
    monkeypatch.setitem(jobs.KINDS, "refetch", ["chat"])
    for name in "ab":
        monkeypatch.setitem(jobs.STEPS, name, lambda ctx: None)
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        c.app = app
        yield c


def _problem(r: httpx.Response, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert (body["status"], body["code"], body["request_id"]) == (status, code, r.headers["x-request-id"])
    return body


async def test_callers_are_refused_as_problems(admin):
    _problem(await admin.get("/api/v2/jobs"), 401, "unauthenticated")
    _problem(await admin.get("/api/v2/jobs", headers={"Authorization": "Bearer nope"}), 403, "forbidden")
    admin.cookies.set(SESSION_COOKIE, "gone")
    _problem(await admin.get("/api/v2/jobs"), 401, "unauthenticated")
    admin.cookies.clear()
    # v1 answers as it always has.
    r = await admin.get("/admin/jobs")
    assert (r.status_code, r.json()) == (403, {"error": True, "msg": "Missing auth key"})


async def test_a_session_write_without_csrf_is_refused_and_audited(admin):
    session = await admin.app.state.admin_sessions.login("twitch:100", user={"login": "alice"})
    admin.cookies.set(SESSION_COOKIE, session.token)
    r = await admin.post("/api/v2/jobs", json={"kind": "test", "subject": f"vod:{VOD}"})
    _problem(r, 403, "forbidden")
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.request_id == r.headers["x-request-id"]))).one()
    assert (row.action, row.outcome, row.actor_kind, row.actor_id, row.actor_login, row.detail) == (
        "request.denied", "denied", "user", "100", "alice",
        {"method": "POST", "path": "/api/v2/jobs", "status": 403})

    r = await admin.post("/api/v2/jobs", headers={CSRF_HEADER: session.csrf},
                         json={"kind": "test", "subject": f"vod:{VOD}", "paused": True})
    assert r.status_code == 201, r.text
    assert r.json()["actor"] == {"kind": "user", "id": "100", "login": "alice", "via": "web"}


async def test_jobs_queue_list_cancel_and_audit_with_the_request_id(admin):
    r = await admin.post("/api/v2/jobs", headers={**KEY, "X-Request-ID": "req-1"},
                         json={"kind": "test", "subject": f"vod:{VOD}", "paused": True})
    assert (r.status_code, r.headers["x-request-id"]) == (201, "req-1")
    run = r.json()
    assert (run["kind"], run["state"], run["step"], run["steps"], run["actor"]["kind"]) == (
        "test", "paused", "a", ["a", "b"], "api_key")

    page = (await admin.get(f"/api/v2/jobs?subject=vod:{VOD}", headers=KEY)).json()
    assert ([j["id"] for j in page["items"]], page["next_cursor"]) == ([run["id"]], None)
    cancel = await admin.post(f"/api/v2/jobs/{run['id']}/cancel", headers=KEY)
    assert cancel.json()["state"] == "cancelled"
    _problem(await admin.post(f"/api/v2/jobs/{run['id']}/resume", headers=KEY), 409, "job_conflict")
    _problem(await admin.get("/api/v2/jobs/999999999", headers=KEY), 404, "job_not_found")

    audit = (await admin.get(f"/api/v2/audit?target=job:{run['id']}", headers=KEY)).json()["items"]
    assert [(a["action"], a["actor_kind"], a["request_id"]) for a in audit] == [
        ("job.cancel", "api_key", cancel.headers["x-request-id"]),
        ("job.enqueue", "api_key", "req-1"),
    ]
    assert audit[1]["at"].endswith("Z")


async def test_queueing_checks_the_vod(admin):
    body = {"kind": "test", "paused": True}
    r = await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": "channel:1"})
    _problem(r, 422, "invalid_subject")
    _problem(await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": "vod:nope"}), 404, "vod_not_found")
    _problem(await admin.post("/api/v2/jobs", headers=KEY, json={**body, "kind": "nope", "subject": f"vod:{VOD}"}),
             422, "invalid_job")
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).merged_into = {"id": "other"}
        await s.commit()
    r = await admin.post("/api/v2/jobs", headers=KEY, json={"kind": "refetch", "subject": f"vod:{VOD}"})
    assert "merged into other" in _problem(r, 409, "vod_spliced")["detail"]
    # A kind that does not refetch from Twitch is still fine on a merged VOD.
    assert (await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": f"vod:{VOD}"})).status_code == 201


async def test_invalid_bodies_list_their_errors(admin):
    body = _problem(await admin.post("/api/v2/jobs", headers=KEY, json={"subject": 1}), 422, "invalid")
    assert ["body", "kind"] in [e["loc"] for e in body["errors"]]


async def test_docs_are_behind_auth_and_cover_v2_only(admin):
    _problem(await admin.get("/api/v2/docs"), 401, "unauthenticated")
    _problem(await admin.get("/api/v2/openapi.json"), 401, "unauthenticated")
    assert "swagger-ui" in (await admin.get("/api/v2/docs", headers=KEY)).text
    paths = (await admin.get("/api/v2/openapi.json", headers=KEY)).json()["paths"]
    assert {"/api/v2/jobs", "/api/v2/jobs/{run_id}/cancel", "/api/v2/audit", "/api/v2/job-kinds"} <= set(paths)
    assert not [p for p in paths if not p.startswith("/api/v2/") or p.endswith(("/docs", "/openapi.json"))]
    assert (await admin.get("/openapi.json", headers=KEY)).status_code == 404  # still off for the rest
