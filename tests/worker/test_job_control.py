"""Manual step gates, pause/resume/cancel, and the admin job endpoints (needs the dev DB)."""

import asyncio
import datetime as dt

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete

from archive_common.db import get_sessionmaker
from archive_common.models import Job, Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app

VOD = "test-job-control-vod"


async def _reset():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id == VOD))
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


async def _job(job_id: int) -> Job:
    async with get_sessionmaker()() as s:
        return await s.get(Job, job_id)


async def _claim_and_run(runner: jobs.Runner, job_id: int) -> Job:
    job = await runner._claim()
    assert job is not None and job.id == job_id
    await runner._run(job)
    return await _job(job_id)


async def test_global_gate_pauses_until_resumed(vod, steps, deps):
    deps.settings.manual_steps = {"test": ["b"]}
    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", vod, settings=deps.settings)

    after = await _claim_and_run(runner, job.id)
    assert (after.state, after.step, steps) == ("paused", "b", ["a"])
    assert await runner._claim() is None  # paused jobs are not picked up

    assert (await jobs.resume(job.id)).state == "queued"
    done = await _claim_and_run(runner, job.id)
    assert (done.state, steps) == ("done", ["a", "b", "c"])


async def test_job_override_beats_global_and_gates_first_step(vod, steps, deps):
    deps.settings.manual_steps = {"test": ["b"]}
    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", vod, pause_before=["a"], settings=deps.settings)
    assert (job.state, job.step) == ("paused", "a")

    assert (await jobs.resume(job.id)).state == "queued"
    done = await _claim_and_run(runner, job.id)
    assert (done.state, steps) == ("done", ["a", "b", "c"])  # global gate on b ignored


async def test_resume_once_single_steps(vod, steps, deps):
    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", vod, paused=True, settings=deps.settings)

    assert (await jobs.resume(job.id, once=True)).state == "queued"
    after = await _claim_and_run(runner, job.id)
    assert (after.state, after.step, after.pause_next, steps) == ("paused", "b", False, ["a"])

    assert (await jobs.resume(job.id)).state == "queued"
    assert (await _claim_and_run(runner, job.id)).state == "done"


async def test_pause_requested_while_running(vod, deps, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        started.set()
        await release.wait()

    async def fast(ctx):
        pass

    monkeypatch.setitem(jobs.KINDS, "test", ["slow", "fast"])
    monkeypatch.setitem(jobs.STEPS, "slow", slow)
    monkeypatch.setitem(jobs.STEPS, "fast", fast)
    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", vod, settings=deps.settings)
    claimed = await runner._claim()
    task = asyncio.create_task(runner._run(claimed))
    await started.wait()

    await jobs.pause(job.id)
    release.set()
    await task
    after = await _job(job.id)
    assert (after.state, after.step, after.pause_next) == ("paused", "fast", False)


async def test_cancel_running_job(vod, deps, monkeypatch):
    started = asyncio.Event()

    async def forever(ctx):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(jobs.KINDS, "test", ["forever"])
    monkeypatch.setitem(jobs.STEPS, "forever", forever)
    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", vod, settings=deps.settings)
    await runner._fill()
    await started.wait()

    cancelled = await runner.cancel(job.id)
    assert cancelled.state == "cancelled"
    assert job.id not in runner.running


def test_unknown_manual_step_fails_at_startup(deps):
    deps.settings.manual_steps = {"archive": ["uplaod"]}
    with pytest.raises(ValueError, match="uplaod"):
        jobs.Runner(deps)


async def test_admin_launch_list_pause_resume(vod, steps, deps):
    deps.settings.admin_api_key = SecretStr("k")
    runner = jobs.Runner(deps)
    app = create_admin_app(deps, runner)
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

        await _claim_and_run(runner, job_id)
        stopped = (await c.get(f"/admin/jobs?state=stopped&vodId={vod}", headers=headers)).json()
        assert [(j["id"], j["state"], j["step"]) for j in stopped["data"]] == [(job_id, "paused", "c")]
        assert steps == ["b"]

        assert (await c.post(f"/admin/jobs/{job_id}/resume", headers=headers)).status_code == 200
        assert (await c.post(f"/admin/jobs/{job_id}/resume", headers=headers)).status_code == 409
        assert (await _claim_and_run(runner, job_id)).state == "done"
