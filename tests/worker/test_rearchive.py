"""A re-archive's upload step: new parts first, then the uploads they replace are deleted (needs the dev DB)."""

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker import ffmpeg
from archive_worker.context import StepDeferred, StepRefused
from archive_worker.steps import publish
from archive_worker.youtube import YouTubeLimitError
from googleapiclient.errors import HttpError
from sqlalchemy import delete

VOD = "test-rearchive-vod"


@pytest.fixture
async def vod(db):
    async with get_sessionmaker()() as s:
        await s.execute(delete(Vod).where(Vod.id == VOD))
        s.add(
            Vod(
                id=VOD,
                title="t",
                created_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
                duration="03:00:00",
                youtube=[
                    {"id": "old1", "type": "vod", "duration": 3600, "part": 1},
                    {"id": "old2", "type": "vod", "duration": 3600, "part": 2},
                    {"id": "old3", "type": "vod", "duration": 3600, "part": 3},
                    {"id": "live1", "type": "live", "duration": 10800, "part": 1},
                ],
            )
        )
        await s.commit()
    yield VOD
    async with get_sessionmaker()() as s:
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


@pytest.fixture
def fake_youtube(deps, monkeypatch):
    """Uploads get ids new1, new2 …; deleting ``refuse`` fails."""
    state: dict[str, Any] = {"deleted": [], "refuse": set(), "n": 0}

    async def upload(path, **_: Any) -> dict[str, Any]:
        state["n"] += 1
        return {"id": f"new{state['n']}"}

    async def delete_video(video_id: str) -> bool:
        if video_id in state["refuse"]:
            raise RuntimeError("quota exceeded")
        state["deleted"].append(video_id)
        return True

    async def duration(path) -> float:
        return 5400.0

    monkeypatch.setattr(deps.youtube, "upload", upload)
    monkeypatch.setattr(deps.youtube, "delete", delete_video)
    monkeypatch.setattr(ffmpeg, "probe_duration", duration)
    return state


def _payload(numbers: list[int], total: int, **extra: Any) -> dict[str, Any]:
    parts = [{"number": n, "start": (n - 1) * 5400, "end": n * 5400, "path": f"p{n}.mp4"} for n in numbers]
    return {"type": "vod", "replace": True, "parts": parts, "total_parts": total, **extra}


async def _youtube() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as s:
        row = await s.get(Vod, VOD)
        assert row is not None
        return sorted(row.youtube, key=lambda e: (e["type"], e["part"]))


async def test_a_whole_rearchive_in_fewer_parts_deletes_every_old_upload(vod, make_ctx, fake_youtube):
    fake_youtube["refuse"].add("old3")
    ctx = make_ctx("download", VOD, _payload([1, 2], 2))
    await publish.upload(ctx)
    assert fake_youtube["deleted"] == ["old1", "old2"]
    assert ctx.payload["not_removed"] == ["old3"]
    # old3 couldn't be deleted, so it stays listed for the dashboard; the live copy isn't touched.
    assert [(e["type"], e["part"], e["id"]) for e in await _youtube()] == [
        ("live", 1, "live1"),
        ("vod", 1, "new1"),
        ("vod", 2, "new2"),
        ("vod", 3, "old3"),
    ]

    await publish.upload(ctx)  # run again (a retry): nothing is uploaded or deleted twice
    assert fake_youtube["n"] == 2 and fake_youtube["deleted"] == ["old1", "old2"]


async def test_a_one_part_rearchive_replaces_only_that_part(vod, make_ctx, fake_youtube):
    await publish.upload(make_ctx("reupload", VOD, _payload([2], 3, start_part=2, end_part=2)))
    assert fake_youtube["deleted"] == ["old2"]
    assert [e["id"] for e in await _youtube() if e["type"] == "vod"] == ["old1", "new1", "old3"]


async def test_an_upload_without_replace_deletes_nothing(vod, make_ctx, fake_youtube):
    payload = _payload([1], 3, start_part=1, end_part=1)
    del payload["replace"]
    await publish.upload(make_ctx("reupload", VOD, payload))
    assert fake_youtube["deleted"] == []


async def test_a_youtube_limit_reschedules_the_job_then_fails_it(vod, make_ctx, fake_youtube, monkeypatch):
    limit = HttpError(SimpleNamespace(status=400, reason="x"), b"{}")
    allowed = {"n": 1}  # uploads YouTube takes before the limit hits

    async def upload(path, **_: Any) -> dict[str, Any]:
        if allowed["n"] == 0:
            raise YouTubeLimitError("uploadLimitExceeded", "videos.insert", limit)
        allowed["n"] -= 1
        fake_youtube["n"] += 1
        return {"id": f"new{fake_youtube['n']}"}

    ctx = make_ctx("download", VOD, _payload([1, 2, 3], 3))
    monkeypatch.setattr(ctx.deps.youtube, "upload", upload)
    with pytest.raises(StepDeferred) as deferred:
        await publish.upload(ctx)
    assert "(1 of 3)" in str(deferred.value)
    assert deferred.value.until > dt.datetime.now(dt.UTC) + dt.timedelta(hours=23)
    assert list(ctx.payload["uploaded"]) == ["1"] and ctx.payload["youtube_deferrals"] == 1

    allowed["n"] = 1  # the next day part 2 goes through, which starts the count again
    with pytest.raises(StepDeferred):
        await publish.upload(ctx)
    assert list(ctx.payload["uploaded"]) == ["1", "2"] and ctx.payload["youtube_deferrals"] == 1

    for n in (2, 3):
        with pytest.raises(StepDeferred) as deferred:
            await publish.upload(ctx)
        assert f"({n} of 3)" in str(deferred.value)
    with pytest.raises(StepRefused, match="already rescheduled 3 times"):
        await publish.upload(ctx)
    assert ctx.payload["youtube_deferrals"] == 0  # a manual retry gets three more
    assert fake_youtube["n"] == 2
