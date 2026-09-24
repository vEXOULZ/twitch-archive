from __future__ import annotations

import pytest

from archive_common.config import Settings
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix
from archive_worker.context import Deps, JobContext
from archive_worker.youtube import YouTube


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
def make_ctx(deps):
    def make(kind: str = "archive", vod_id: str | None = "100", payload: dict | None = None, job_id: int = 0):
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
