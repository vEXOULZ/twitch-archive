"""Manual step gates, pause/resume/cancel, and the admin job endpoints (needs the dev DB).

New jobs are runs of the job runtime; the legacy table's jobs are still finished by the old runner
(legacy_jobs.py) and reached by the same admin actions.
"""

import asyncio
import datetime as dt

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, text

from archive_common.audit import AUDIT_LOG, actor_of
from archive_common.db import get_sessionmaker
from archive_common.models import Job, Vod
from archive_worker import jobs, legacy_jobs
from archive_worker.admin import create_admin_app
from archive_worker.job_rows import RUNS, subject_of

VOD = "test-job-control-vod"


async def _reset():
    async with get_sessionmaker()() as s:
        ids = select(RUNS.c.id).where(RUNS.c.subject == subject_of(VOD)).union_all(
            select(Job.id).where(Job.vod_id == VOD))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.in_(select(text("'job:' || id")).select_from(
            ids.subquery()))))
        await s.execute(delete(Job).where(Job.vod_id == VOD))
        await s.execute(delete(RUNS).where(RUNS.c.subject == subject_of(VOD)))
        # A retry waiting out its backoff would hold the VOD's lock for the next test.
        # (procrastinate's own SQL names its tables unqualified).
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
def steps(monkeypatch):
    """Kind "test" = steps a, b, c that record their calls."""
    calls: list[str] = []

    def step(name):
        async def run(ctx):
            calls.append(name)
            ctx.payload[name] = True

        return run

    monkeypatch.setitem(jobs.KINDS, "test", ["a", "b", "c"])
    for name in "abc":
        monkeypatch.setitem(jobs.STEPS, name, step(name))
    return calls


# ── Runs of the runtime ───────────────────────────────────────────────────


async def test_global_gate_pauses_until_resumed(vod, steps, deps, make_service, wait_job):
    deps.settings.manual_steps = {"test": ["b"]}
    service = await make_service()
    job = await service.enqueue("test", vod)

    after = await wait_job(job.id, "paused")
    assert (after.step, steps, after.legacy) == ("b", ["a"], False)

    assert (await service.resume(job.id)).state in ("queued", "running", "done")
    done = await wait_job(job.id, "done")
    assert (steps, done.payload) == (["a", "b", "c"], {"a": True, "b": True, "c": True})


async def test_job_override_beats_global_and_gates_first_step(vod, steps, deps, make_service, wait_job):
    deps.settings.manual_steps = {"test": ["b"]}
    service = await make_service()
    job = await service.enqueue("test", vod, pause_before=["a"])
    assert (job.state, job.step) == ("paused", "a")

    await service.resume(job.id)
    await wait_job(job.id, "done")
    assert steps == ["a", "b", "c"]  # global gate on b ignored


async def test_settings_gates_apply_to_queued_jobs(vod, steps, deps, make_service, wait_job):
    service = await make_service(start=False)
    job = await service.enqueue("test", vod)
    deps.settings.manual_steps = {"test": ["c"]}
    service.apply_settings()  # what PATCH /admin/settings does
    await service.runtime.start()
    assert (await wait_job(job.id, "paused")).step == "c"


async def test_resume_once_single_steps(vod, steps, make_service, wait_job):
    service = await make_service()
    job = await service.enqueue("test", vod, paused=True)

    await service.resume(job.id, once=True)
    after = await wait_job(job.id, "paused")
    assert (after.step, after.pause_next, steps) == ("b", False, ["a"])

    await service.resume(job.id)
    await wait_job(job.id, "done")


async def test_pause_requested_while_running(vod, monkeypatch, make_service, wait_job):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        started.set()
        await release.wait()

    async def fast(ctx):
        pass

    monkeypatch.setitem(jobs.KINDS, "test", ["slow", "fast"])
    monkeypatch.setitem(jobs.STEPS, "slow", slow)
    monkeypatch.setitem(jobs.STEPS, "fast", fast)
    service = await make_service()
    job = await service.enqueue("test", vod)
    await asyncio.wait_for(started.wait(), 10)

    await service.pause(job.id)
    release.set()
    after = await wait_job(job.id, "paused")
    assert (after.step, after.pause_next) == ("fast", False)


async def test_cancel_running_job(vod, monkeypatch, make_service, wait_job):
    started = asyncio.Event()

    async def forever(ctx):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(jobs.KINDS, "test", ["forever"])
    monkeypatch.setitem(jobs.STEPS, "forever", forever)
    service = await make_service()
    job = await service.enqueue("test", vod)
    await asyncio.wait_for(started.wait(), 10)

    await service.cancel(job.id)
    await wait_job(job.id, "cancelled")
    assert service.running == 0


async def test_steps_see_the_vod_and_save_the_payload(vod, monkeypatch, make_service, wait_job):
    seen = []

    async def look(ctx):
        seen.append((ctx.vod_id, ctx.kind, ctx.step, (await ctx.get_vod()).id))
        ctx.payload["n"] = 1
        await ctx.save()
        ctx.progress(1, 2, "parts", "half")

    monkeypatch.setitem(jobs.KINDS, "test", ["look"])
    monkeypatch.setitem(jobs.STEPS, "look", look)
    service = await make_service()
    job = await service.enqueue("test", vod)
    assert (await wait_job(job.id, "done")).payload == {"n": 1}
    assert seen == [(VOD, "test", "look", VOD)]
    events = await service.events(job.id)
    assert {"done": 1, "total": 2, "unit": "parts"} in [e["progress"] for e in events]


def test_unknown_manual_step_fails_at_startup(deps):
    deps.settings.manual_steps = {"archive": ["uplaod"]}
    with pytest.raises(ValueError, match="uplaod"):
        jobs.create_runtime(deps)


# ── Jobs of the legacy table ──────────────────────────────────────────────


async def _legacy_job(kind: str, **values) -> Job:
    """A job as the old runner's ``enqueue`` left it: nothing queues these any more."""
    async with get_sessionmaker()() as s:
        job = Job(kind=kind, vod_id=VOD, step=jobs.KINDS[kind][0], state="queued", payload={}, **values)
        s.add(job)
        await s.commit()
        return job


async def _claim_and_run(runner: legacy_jobs.Runner, job_id: int):
    job = await runner._claim()
    assert job is not None and job.id == job_id
    await runner._run(job)
    return await jobs.get(job_id)


async def test_legacy_job_drains_through_its_gates(vod, steps, deps, make_service):
    deps.settings.manual_steps = {"test": ["b"]}
    service = await make_service(start=False)
    job = await _legacy_job("test")

    after = await _claim_and_run(service.legacy, job.id)
    assert (after.state, after.step, after.legacy, steps) == ("paused", "b", True, ["a"])
    assert await service.legacy._claim() is None  # paused jobs are not picked up

    assert (await service.resume(job.id)).state == "queued"
    done = await _claim_and_run(service.legacy, job.id)
    assert (done.state, steps) == ("done", ["a", "b", "c"])


async def test_legacy_job_cancelled_while_running(vod, deps, monkeypatch, make_service):
    started = asyncio.Event()

    async def forever(ctx):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(jobs.KINDS, "test", ["forever"])
    monkeypatch.setitem(jobs.STEPS, "forever", forever)
    service = await make_service(start=False)
    job = await _legacy_job("test")
    await service.legacy._fill()
    await asyncio.wait_for(started.wait(), 10)

    assert (await service.cancel(job.id)).state == "cancelled"
    assert job.id not in service.legacy.running


# ── Admin endpoints ───────────────────────────────────────────────────────


async def test_admin_launch_list_pause_resume(vod, steps, deps, make_service, wait_job):
    deps.settings.admin_api_key = SecretStr("k")
    service = await make_service(start=False)
    app = create_admin_app(deps, service)
    headers = {"Authorization": "Bearer k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        kinds = (await c.get("/admin/kinds", headers=headers)).json()
        assert kinds["test"]["steps"] == ["a", "b", "c"]

        bad = await c.post("/admin/jobs", headers=headers, json={"kind": "test", "vodId": vod, "fromStep": "z"})
        assert bad.status_code == 400 and "no step(s) z" in bad.json()["msg"]

        r = await c.post("/admin/jobs", headers=headers,
                         json={"kind": "test", "vodId": vod, "fromStep": "b", "pauseBefore": ["c"]})
        job_id = r.json()["jobId"]
        listed = (await c.get(f"/admin/jobs?state=waiting&kind=test&vodId={vod}", headers=headers)).json()
        assert [j["id"] for j in listed["data"]] == [job_id]
        assert listed["data"][0]["pauseBefore"] == ["c"]

        await service.runtime.start()
        await wait_job(job_id, "paused")
        stopped = (await c.get(f"/admin/jobs?state=stopped&vodId={vod}", headers=headers)).json()
        assert [(j["id"], j["state"], j["step"]) for j in stopped["data"]] == [(job_id, "paused", "c")]
        assert steps == ["b"]

        assert (await c.post(f"/admin/jobs/{job_id}/resume", headers=headers)).status_code == 200
        r = await c.post(f"/admin/jobs/{job_id}/resume", headers=headers)
        assert r.status_code == 409 and r.json()["msg"].startswith("Job is ")
        await wait_job(job_id, "done")
        events = (await c.get(f"/admin/jobs/{job_id}/events", headers=headers)).json()
        assert f"Job {job_id} resumed at step c" in [e["message"] for e in events["data"]]


async def test_admin_job_routes_404_and_409(vod, steps, deps, make_service, wait_job):
    deps.settings.admin_api_key = SecretStr("k")
    service = await make_service()
    app = create_admin_app(deps, service)
    headers = {"Authorization": "Bearer k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        missing = 2**62
        for method, path in (("get", ""), ("post", "/resume"), ("post", "/pause"), ("post", "/retry"),
                             ("post", "/cancel"), ("patch", ""), ("get", "/events")):
            kwargs = {"json": {"pauseNext": True}} if method == "patch" else {}
            r = await c.request(method.upper(), f"/admin/jobs/{missing}{path}", headers=headers, **kwargs)
            assert (r.status_code, r.json()["msg"]) == (404, "No such job"), path

        job = await service.enqueue("test", vod)
        await wait_job(job.id, "done")
        r = await c.post(f"/admin/jobs/{job.id}/pause", headers=headers)
        assert (r.status_code, r.json()["msg"]) == (409, "Job is done; only queued or running jobs can be paused")

        legacy = await _legacy_job("test")
        async with get_sessionmaker()() as s:
            (await s.get(Job, legacy.id)).state = "done"
            await s.commit()
        r = await c.post(f"/admin/jobs/{legacy.id}/pause", headers=headers)
        assert (r.status_code, r.json()["msg"]) == (409, "Job is done; only queued or running jobs can be paused")


# ── Audit ─────────────────────────────────────────────────────────────────


async def _audited(job_id: int) -> list[tuple]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(
            select(AUDIT_LOG).where(AUDIT_LOG.c.target == f"job:{job_id}").order_by(AUDIT_LOG.c.id)
        )).all()
    return [(r.action, r.actor_kind, r.via, r.before, r.after) for r in rows]


async def test_admin_job_actions_are_audited_once_with_the_caller(vod, steps, deps, make_service):
    deps.settings.admin_api_key = SecretStr("k")
    service = await make_service(start=False)
    app = create_admin_app(deps, service)
    headers = {"Authorization": "Bearer k"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        r = await c.post("/admin/jobs", headers=headers, json={"kind": "test", "vodId": vod, "paused": True})
        job_id = r.json()["jobId"]
        assert (await c.post(f"/admin/jobs/{job_id}/cancel", headers=headers)).status_code == 200
    # The runtime's rows, in the transaction of each change; none from the request as well.
    assert await _audited(job_id) == [
        ("job.enqueue", "api_key", "api", None, {"kind": "test", "step": "a", "state": "paused", "subject": f"vod:{vod}"}),
        ("job.cancel", "api_key", "api", {"state": "paused"}, None),
    ]


async def test_legacy_job_actions_are_audited_by_the_service(vod, steps, deps, make_service):
    service = await make_service(start=False)
    job = await _legacy_job("test")
    await service.pause(job.id, actor=actor_of("twitch:100", "alice"))
    assert await _audited(job.id) == [("job.pause", "user", "web", {"state": "queued"}, {"state": "paused"})]
