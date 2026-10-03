from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from archive_common.config import Settings
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix
from archive_worker import jobs, legacy_jobs
from archive_worker.context import Deps, JobContext
from archive_worker.youtube import YouTube

if sys.platform == "win32":
    # The job runtime's psycopg pool needs a selector loop on Windows (asyncpg works on either).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        twitch_id="38656648",
        twitch_username="vexoulz",
        channel="vexoulz",
        domain_name="vods.example.net",
        restricted_games=["Artifact"],
        split_duration=10800,
        hls_poll_interval_seconds=0,
    )


@pytest.fixture
def deps(settings) -> Deps:
    return Deps(settings, Helix(settings), Gql(settings), YouTube(settings))


@pytest.fixture
def api():
    """The public archive API, rate limit and service cache off."""
    import httpx
    from archive_api.main import create_app

    app = create_app(Settings(rate_limit_points=1_000_000, cache_ttl_seconds=0))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


@pytest.fixture
def make_ctx(deps):
    def make(kind: str = "archive", vod_id: str | None = "100", payload: dict[str, Any] | None = None, job_id: int = 0):
        return JobContext(job_id, kind, vod_id, dict(payload or {}), deps)

    return make


@pytest.fixture
async def db():
    """Local Postgres (see README "Development"); tests using it skip without one."""
    import sqlalchemy
    from archive_common.db import get_engine

    try:
        async with get_engine().connect() as conn:
            await conn.execute(sqlalchemy.text("select 1 from jobs limit 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")
    return get_engine()


@pytest.fixture
async def make_service(deps, db):
    """``await make_service(start=True)``: a JobService whose runtime runs the kinds in ``jobs.KINDS``
    (a test's monkeypatched ones too) on the dev DB, closed after the test. ``start=False`` only
    opens it: jobs are queued but nothing runs them."""
    made: list[Any] = []

    async def make(*, start: bool = True) -> jobs.JobService:
        runtime = jobs.create_runtime(deps, poll_interval=0.2, shutdown_timeout=2.0)
        await runtime.open()
        made.append(runtime)
        if start:
            await runtime.start()
        return jobs.JobService(deps, runtime, legacy_jobs.Runner(deps, runtime.registry))

    yield make
    for runtime in made:
        await runtime.close()


@pytest.fixture
def wait_job():
    """``await wait_job(job_id, "done", ...)``: the job (as ``jobs.get`` reads it) once in one of the states."""

    async def wait(job_id: int, *states: str, timeout: float = 15.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            job = await jobs.get(job_id)
            if job.state in states:
                return job
            if loop.time() > deadline:
                raise AssertionError(f"job {job_id} still {job.state} ({job.last_error}); wanted {states}")
            await asyncio.sleep(0.05)

    return wait
