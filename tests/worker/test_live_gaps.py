"""A live recording's gaps (ad breaks) on its uploads: computed at upload, kept on re-upload, backfilled."""

import datetime as dt
from typing import Any

import httpx
import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import Job, Vod
from archive_worker import jobs, planning, timeline
from archive_worker.admin import create_admin_app
from archive_worker.job_rows import RUNS
from archive_worker.steps import publish
from pydantic import SecretStr
from sqlalchemy import delete, select

VOD = "test-live-gaps-vod"
START = dt.datetime(2026, 10, 1, 0, 20, 9, tzinfo=dt.UTC)

# Like VOD 2895611509: 15030 s long, recorded from 47 s in, a 150 s ad break every ~1700 s, and
# the recording cut into a 10800 s part and the rest.
STRETCH = 13624.861 / 8
TIMELINE = [[i * STRETCH, START.timestamp() + 47 + i * (STRETCH + 150), STRETCH] for i in range(8)]
PARTS = [planning.Part(1, 0, 10800), planning.Part(2, 10800, 13624.861)]


def test_gaps_of_the_example_leave_47_s_of_delay():
    gaps = planning.part_gaps(15030, START.timestamp(), [], [], TIMELINE, PARTS)
    assert [len(gaps[1]), len(gaps[2])] == [6, 2]  # 7 breaks between 8 stretches, then an early end
    assert gaps[1][0] == [planning.num_seconds(STRETCH), 150]
    youtube = [
        {"id": "p1", "type": "live", "part": 1, "duration": 10800, "gaps": gaps[1]},
        {"id": "p2", "type": "live", "part": 2, "duration": 2824.861, "gaps": gaps[2]},
    ]
    t = timeline.Timeline.of(VOD, 15030, [], youtube, "live")
    assert t.delay == pytest.approx(47, abs=0.01)
    # Every stretch starts on the VOD where it was recorded, not up to 1200 s too early.
    for rec, wall, _ in TIMELINE:
        assert t.vod_time(rec) == pytest.approx(wall - START.timestamp(), abs=0.01)
    assert t.part_spans()[-1][1] == pytest.approx(15030, abs=0.01)


def test_a_cut_inside_a_gap_is_not_counted_twice():
    cut = {"start": 47 + STRETCH + 100, "end": 200, "restricted": True}  # 50 s of the break, 150 s after
    gaps = planning.part_gaps(15030, START.timestamp(), [cut], [], TIMELINE, PARTS)
    assert gaps[1][0] == [planning.num_seconds(STRETCH), 100]


def test_recorded_gaps_need_every_part_and_map_by_upload_id():
    payload: dict[str, Any] = {
        "timeline": TIMELINE,
        "parts": [{"number": p.number, "start": p.start, "end": p.end, "path": "x"} for p in PARTS],
        "total_parts": 2,
        "uploaded": {"1": {"id": "yt1"}, "2": {"id": "yt2"}},
    }
    gaps = publish.recorded_gaps(payload, 15030, START.timestamp(), [], [])
    assert set(gaps) == {"yt1", "yt2"}
    assert publish.recorded_gaps({**payload, "total_parts": 3}, 15030, START.timestamp(), [], []) == {}
    assert publish.recorded_gaps({**payload, "timeline": []}, 15030, START.timestamp(), [], []) == {}


# ── With the database ─────────────────────────────────────────────────────


async def _reset() -> None:
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id == VOD))
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


@pytest.fixture
async def vod(db):
    await _reset()
    async with get_sessionmaker()() as s:
        s.add(
            Vod(
                id=VOD,
                title="t",
                created_at=START,
                duration="04:10:30",
                chapters=[],
                youtube=[
                    {"id": "v1", "type": "vod", "part": 1, "duration": 15030},
                    {"id": "yt1", "type": "live", "part": 1, "duration": 10800},
                    {"id": "yt2", "type": "live", "part": 2, "duration": 2824.861},
                ],
            )
        )
        await s.commit()
    yield
    await _reset()


async def _youtube() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as s:
        return (await s.execute(select(Vod.youtube).where(Vod.id == VOD))).scalar_one()  # type: ignore[no-any-return]


async def test_backfill_sets_gaps_once(vod, make_ctx):
    payload = {
        "timeline": TIMELINE,
        "parts": [{"number": p.number, "start": p.start, "end": p.end, "path": "x"} for p in PARTS],
        "total_parts": 2,
        "uploaded": {"1": {"id": "yt1"}, "2": {"id": "yt2"}},
    }
    async with get_sessionmaker()() as s:
        s.add(Job(kind="live", vod_id=VOD, step=None, state="done", payload=payload))
        await s.commit()

    await publish.live_gaps_backfill(make_ctx("live_gaps_backfill", None, {"vod_ids": [VOD]}))
    youtube = await _youtube()
    assert "gaps" not in youtube[0]  # the vod upload has the whole VOD
    assert [len(e["gaps"]) for e in youtube[1:]] == [6, 2]

    async with get_sessionmaker()() as s:  # an edit by hand since is kept: a VOD with gaps is skipped
        row = await s.get(Vod, VOD)
        row.youtube = [*youtube[:2], {**youtube[2], "gaps": []}]
        await s.commit()
    await publish.live_gaps_backfill(make_ctx("live_gaps_backfill", None, {"vod_ids": [VOD]}))
    assert (await _youtube())[2]["gaps"] == []


async def test_a_reupload_keeps_the_gaps_of_the_upload_it_replaces(vod):
    await publish._save_entry(VOD, {"id": "yt1", "type": "live", "part": 1, "duration": 10800, "gaps": [[100, 150]]})
    await publish._save_entry(VOD, {"id": "yt1b", "type": "live", "part": 1, "duration": 10800})
    entry = next(e for e in await _youtube() if e["type"] == "live" and e["part"] == 1)
    assert entry["id"] == "yt1b" and entry["gaps"] == [[100, 150]]


async def test_admin_live_gaps_backfill_route(vod, deps, make_service):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    headers = {"Authorization": "Bearer k"}
    job_id = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
            assert (await c.post("/admin/live-gaps/backfill", headers=headers, json={"vodIds": []})).status_code == 400
            started = await c.post("/admin/live-gaps/backfill", headers=headers, json={"vodIds": [VOD]})
            job_id = started.json()["jobId"]
            assert (await c.post("/admin/live-gaps/backfill", headers=headers)).status_code == 409
        job = await jobs.get(job_id)
        assert (job.kind, job.vod_id, job.payload) == ("live_gaps_backfill", None, {"vod_ids": [VOD]})
    finally:
        if job_id is not None:
            async with get_sessionmaker()() as s:
                await s.execute(delete(RUNS).where(RUNS.c.id == job_id))
                await s.commit()
