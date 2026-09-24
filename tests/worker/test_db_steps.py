"""Steps and the job runner against the local Postgres (skipped without it).

Assumes a dev database with no other queued jobs (see README "Development").
"""

import asyncio
import datetime as dt
import json

import httpx
import respx
from sqlalchemy import delete, select

from archive_common.db import get_sessionmaker
from archive_common.models import Job, Log, Vod
from archive_common.twitch.gql import GQL_URL
from archive_worker import jobs
from archive_worker.steps import metadata

VOD = "test-worker-vod"


async def _reset():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Log).where(Log.vod_id == VOD))
        await s.execute(delete(Job).where(Job.vod_id == VOD))
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


async def _vod():
    await _reset()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=VOD, title="t", created_at=dt.datetime.now(dt.timezone.utc), duration="00:10:00"))
        await s.commit()


def _node(cid: str, offset: int, color: str | None = "#FF0000"):
    return {
        "id": cid,
        "contentOffsetSeconds": offset,
        "createdAt": "2026-02-21T03:00:00.123Z",
        "commenter": {"displayName": "someone"},
        "message": {"fragments": [{"text": "hi", "emote": None}], "userBadges": [], "userColor": color},
    }


PAGES = {
    None: {
        "edges": [
            {"cursor": "c1", "node": _node("00000000-0000-0000-0000-000000000001", 1)},
            {"cursor": "c2", "node": _node("00000000-0000-0000-0000-000000000002", 5, None)},
        ],
        "pageInfo": {"hasNextPage": True},
    },
    "c2": {
        "edges": [{"cursor": "c3", "node": _node("00000000-0000-0000-0000-000000000003", 9)}],
        "pageInfo": {"hasNextPage": False},
    },
}


def _gql(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    page = PAGES[body["variables"].get("cursor")]
    return httpx.Response(200, json={"data": {"video": {"comments": page}}})


async def _nosleep(*_a, **_k):
    return None


@respx.mock
async def test_chat_crawl_is_idempotent(db, make_ctx, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    await _vod()
    route = respx.post(GQL_URL).mock(side_effect=_gql)
    ctx = make_ctx("chat", VOD)
    await metadata.chat(ctx)
    await metadata.chat(ctx)  # resumes from the max stored offset; duplicates are ignored
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(Log).where(Log.vod_id == VOD).order_by(Log.seq))).scalars().all()
    assert [r.content_offset_seconds for r in rows] == [1, 5, 9]
    assert rows[1].user_color == "#999999"
    assert rows[0].message == [{"text": "hi", "emote": None}]
    # a run starts by offset (main client) and continues by cursor (backup client)
    first, second = route.calls[0].request, route.calls[1].request
    assert json.loads(first.content)["variables"] == {"videoID": VOD, "contentOffsetSeconds": 0}
    assert json.loads(second.content)["variables"] == {"videoID": VOD, "cursor": "c2"}
    assert first.headers["Client-Id"] != second.headers["Client-Id"]
    assert json.loads(route.calls[2].request.content)["variables"]["contentOffsetSeconds"] == 9
    await _reset()


async def test_runner_resumes_from_failed_step(db, deps, monkeypatch):
    await _vod()
    calls: list[str] = []
    fail = {"b": 1}

    def step(name):
        async def run(ctx):
            calls.append(name)
            ctx.payload[name] = True
            if fail.get(name):
                fail[name] -= 1
                raise RuntimeError("boom")

        return run

    monkeypatch.setitem(jobs.KINDS, "test", ["a", "b", "c"])
    for name in "abc":
        monkeypatch.setitem(jobs.STEPS, name, step(name))

    runner = jobs.Runner(deps)
    job = await jobs.enqueue("test", VOD)
    await runner._run(job)
    async with get_sessionmaker()() as s:
        after = await s.get(Job, job.id)
    assert (after.state, after.step, after.attempts) == ("queued", "b", 1)
    assert after.payload["a"] and "not_before" in after.payload
    assert "boom" in after.last_error

    await runner._run(after)  # e.g. a restarted worker picks it up again
    async with get_sessionmaker()() as s:
        done = await s.get(Job, job.id)
    assert done.state == "done" and done.step is None
    assert calls == ["a", "b", "b", "c"]
    assert done.payload == {"a": True, "b": True, "c": True}
    await _reset()


async def test_claim_respects_not_before_and_exclusivity(db, deps):
    await _vod()
    runner = jobs.Runner(deps)
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    await jobs.enqueue("emotes", VOD, {"not_before": later})
    assert await runner._claim() is None
    j1 = await jobs.enqueue("emotes", VOD)
    j2 = await jobs.enqueue("chapters", VOD)
    claimed = await runner._claim()
    assert claimed.id == j1.id
    runner.running_keys[claimed.id] = jobs._exclusive_key(claimed)
    assert await runner._claim() is None  # same vod + type is busy
    runner.running_keys.clear()
    assert (await runner._claim()).id == j2.id
    await _reset()
