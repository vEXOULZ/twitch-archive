"""Emotes step: global sets, fill-missing re-runs, and the global backfill (needs the dev DB)."""

import datetime as dt

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import delete

from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Job, Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.context import StepError
from archive_worker.steps import metadata

VOD, OTHER = "test-emotes-vod", "test-emotes-vod-2"
TWITCH_ID = "38656648"  # conftest settings

GLOBAL_7TV = {"id": "global", "emotes": [{"id": "g7", "name": "EZ", "flags": 0}, {"id": "g7z", "name": "RainTime", "flags": 1}]}
GLOBAL_BTTV = [{"id": "gb", "code": "monkaS"}]
GLOBAL_FFZ = {
    "default_sets": [3],
    "sets": {"3": {"emoticons": [{"id": 9, "name": "ZreknarF"}]}, "4330": {"emoticons": [{"id": 1, "name": "notDefault"}]}},
}

EXPECTED_GLOBALS = {
    "7tv": [{"id": "g7", "code": "EZ", "flags": 0}, {"id": "g7z", "code": "RainTime", "flags": 1}],
    "bttv": [{"id": "gb", "code": "monkaS"}],
    "ffz": [{"id": 9, "code": "ZreknarF"}],
}
CHANNEL = {
    "ffz_emotes": [{"id": 1, "code": "ffzNow"}],
    "bttv_emotes": [{"id": "gb", "code": "monkaS"}, {"id": "b1", "code": "bttvNow"}],
    "seventv_emotes": [{"id": "s1", "code": "stvNow", "flags": 0}],
}
OLD = {  # what a VOD saved a year ago holds
    "ffz_emotes": [{"id": 100, "code": "ffzOld"}],
    "bttv_emotes": [{"id": "b100", "code": "bttvOld"}],
    "seventv_emotes": [{"id": "s100", "code": "stvOld", "flags": 0}],
}


def mock_providers(**override):
    urls = {
        "ffz_room": (f"{metadata.FFZ}/room/id/{TWITCH_ID}", {"room": {"set": 111}, "sets": {"111": {"emoticons": [{"id": 1, "name": "ffzNow"}]}}}),
        "bttv_user": (f"{metadata.BTTV}/cached/users/twitch/{TWITCH_ID}", {"channelEmotes": [{"id": "b1", "code": "bttvNow"}], "sharedEmotes": []}),
        "7tv_user": (f"{metadata.SEVENTV}/users/twitch/{TWITCH_ID}", {"emote_set": {"emotes": [{"id": "s1", "name": "stvNow", "flags": 0}]}}),
        "7tv_global": (f"{metadata.SEVENTV}/emote-sets/global", GLOBAL_7TV),
        "bttv_global": (f"{metadata.BTTV}/cached/emotes/global", GLOBAL_BTTV),
        "ffz_global": (f"{metadata.FFZ}/set/global", GLOBAL_FFZ),
    }
    for name, (url, body) in urls.items():
        response = override.get(name, httpx.Response(200, json=body))
        respx.get(url).mock(return_value=response)


async def _reset():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Emote).where(Emote.vod_id.in_([VOD, OTHER])))
        await s.execute(delete(Job).where(Job.vod_id.in_([VOD, OTHER])))
        await s.execute(delete(Vod).where(Vod.id.in_([VOD, OTHER])))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _reset()
    async with get_sessionmaker()() as s:
        for vid in (VOD, OTHER):
            s.add(Vod(id=vid, title="t", created_at=dt.datetime.now(dt.timezone.utc), duration="00:10:00"))
        await s.commit()
    yield
    await _reset()


async def _row(vod_id: str = VOD) -> Emote | None:
    async with get_sessionmaker()() as s:
        return await s.get(Emote, vod_id)


async def _insert(vod_id: str = VOD, **values):
    async with get_sessionmaker()() as s:
        s.add(Emote(vod_id=vod_id, **values))
        await s.commit()


def _channel(row: Emote) -> dict:
    return {k: getattr(row, k) for k in metadata.CHANNEL_SETS}


# ── Without the database (these also run in CI) ───────────────────────────


@respx.mock
async def test_fetch_emotes_parses_globals_and_survives_failures(make_ctx):
    mock_providers()
    fetched = await metadata.fetch_emotes(make_ctx("emotes", VOD), TWITCH_ID)
    assert fetched == {**CHANNEL, "global_emotes": EXPECTED_GLOBALS}

    respx.get(f"{metadata.SEVENTV}/emote-sets/global").mock(return_value=httpx.Response(404))
    respx.get(f"{metadata.FFZ}/set/global").mock(return_value=httpx.Response(200, json={"sets": {}}))
    assert await metadata.fetch_global_emotes(make_ctx("emotes", VOD)) == {
        "7tv": [], "bttv": EXPECTED_GLOBALS["bttv"], "ffz": [],
    }


def test_merge_emotes():
    now = dt.datetime.now(dt.timezone.utc)
    fetched = {**CHANNEL, "global_emotes": EXPECTED_GLOBALS}
    captured = {**CHANNEL, "global_emotes": EXPECTED_GLOBALS, "global_emotes_source": "captured", "global_emotes_at": now}
    old = Emote(vod_id=VOD, **OLD)

    assert metadata.merge_emotes(None, fetched, force=False, now=now) == captured
    assert metadata.merge_emotes(old, fetched, force=True, now=now) == captured
    assert metadata.merge_emotes(old, fetched, force=False, now=now) == {
        "global_emotes": EXPECTED_GLOBALS, "global_emotes_source": "backfilled", "global_emotes_at": now,
    }
    complete = Emote(vod_id=VOD, **OLD, global_emotes=EXPECTED_GLOBALS, global_emotes_source="captured")
    assert metadata.merge_emotes(complete, fetched, force=False, now=now) == {}


# ── Against the dev database ──────────────────────────────────────────────


@respx.mock
async def test_capture_saves_all_three_globals(vods, make_ctx):
    mock_providers()
    await metadata.emotes(make_ctx("emotes", VOD))
    row = await _row()
    assert row.global_emotes == EXPECTED_GLOBALS
    assert row.global_emotes_source == "captured" and row.global_emotes_at is not None
    assert _channel(row) == CHANNEL  # bttv_emotes still mixes the BTTV globals in


@respx.mock
async def test_failing_provider_leaves_its_list_empty(vods, make_ctx):
    mock_providers(**{"7tv_global": httpx.Response(404), "ffz_global": httpx.Response(200, json={"unexpected": 1})})
    await metadata.emotes(make_ctx("emotes", VOD))  # does not raise
    row = await _row()
    assert row.global_emotes == {"7tv": [], "bttv": EXPECTED_GLOBALS["bttv"], "ffz": []}
    assert _channel(row) == CHANNEL


@respx.mock
async def test_rerun_without_force_keeps_existing_channel_sets(vods, make_ctx):
    mock_providers()
    await _insert(**{**OLD, "ffz_emotes": []})  # FFZ failed back then
    await metadata.emotes(make_ctx("emotes", VOD))
    row = await _row()
    assert _channel(row) == {**OLD, "ffz_emotes": CHANNEL["ffz_emotes"]}  # only the empty set is filled
    assert row.global_emotes == EXPECTED_GLOBALS
    assert row.global_emotes_source == "backfilled"  # filled later, not captured with the VOD

    await metadata.emotes(make_ctx("emotes", VOD, {"force": True}))
    row = await _row()
    assert _channel(row) == CHANNEL
    assert row.global_emotes_source == "captured"


@respx.mock
async def test_rerun_fills_only_missing_global_providers(vods, make_ctx):
    mock_providers()
    kept = {"7tv": [{"id": "old7", "code": "OldEZ", "flags": 0}], "bttv": [], "ffz": [{"id": 1, "code": "OldF"}]}
    await _insert(**OLD, global_emotes=kept, global_emotes_source="captured")
    await metadata.emotes(make_ctx("emotes", VOD))
    row = await _row()
    assert row.global_emotes == {**kept, "bttv": EXPECTED_GLOBALS["bttv"]}
    assert _channel(row) == OLD


@respx.mock
async def test_backfill_marks_backfilled_and_is_idempotent(vods, make_ctx):
    mock_providers()
    captured = {"7tv": [{"id": "x", "code": "Then", "flags": 0}], "bttv": [], "ffz": []}
    await _insert(VOD, **OLD)
    await _insert(OTHER, **OLD, global_emotes=captured, global_emotes_source="captured")
    ctx = make_ctx("global_emotes_backfill", None, {"vod_ids": [VOD, OTHER]})

    await metadata.global_emotes_backfill(ctx)
    row, other = await _row(VOD), await _row(OTHER)
    assert row.global_emotes == EXPECTED_GLOBALS and row.global_emotes_source == "backfilled"
    assert _channel(row) == OLD  # channel columns untouched
    assert other.global_emotes == captured and other.global_emotes_source == "captured"

    first_at = row.global_emotes_at
    await metadata.global_emotes_backfill(ctx)
    again = await _row(VOD)
    assert again.global_emotes == EXPECTED_GLOBALS and again.global_emotes_at == first_at


@respx.mock
async def test_backfill_writes_nothing_when_a_provider_fails(vods, make_ctx):
    mock_providers(bttv_global=httpx.Response(404))
    await _insert(VOD, **OLD)
    with pytest.raises(StepError, match="bttv"):
        await metadata.global_emotes_backfill(make_ctx("global_emotes_backfill", None, {"vod_ids": [VOD]}))
    assert (await _row()).global_emotes is None


async def test_admin_emotes_force_and_backfill_routes(vods, deps):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, jobs.Runner(deps))
    headers = {"Authorization": "Bearer k"}
    backfill_id = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
            plain = (await c.post("/admin/emotes", headers=headers, json={"vodId": VOD})).json()
            forced = (await c.post("/admin/emotes", headers=headers, json={"vodId": VOD, "force": True})).json()
            assert (await c.post("/admin/emotes/backfill", headers=headers, json={"vodIds": "x"})).status_code == 400
            backfill = (await c.post("/admin/emotes/backfill", headers=headers, json={"vodIds": [VOD]})).json()
            backfill_id = backfill["jobId"]
            assert (await c.post("/admin/emotes/backfill", headers=headers)).status_code == 409
        assert (await _job(plain["jobId"])).payload == {}
        assert (await _job(forced["jobId"])).payload == {"force": True}
        job = await _job(backfill_id)
        assert (job.kind, job.vod_id, job.payload) == ("global_emotes_backfill", None, {"vod_ids": [VOD]})
    finally:
        if backfill_id is not None:
            async with get_sessionmaker()() as s:
                await s.execute(delete(Job).where(Job.id == backfill_id))
                await s.commit()


async def _job(job_id: int) -> Job:
    async with get_sessionmaker()() as s:
        return await s.get(Job, job_id)
