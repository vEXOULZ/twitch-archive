"""Synthetic VODs through /api/v2 and as archive-api serves them, against the dev DB.

A (2 h) and B (1 h) are one broadcast that Twitch cut in two: B started 7500 s after A, so a merge
has a 300 s gap. A plays Just Chatting then the test game for an hour; B plays the test game.
"""

import datetime as dt
from urllib.parse import quote

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, or_, select, update

from archive_api.games_played import games_played
from archive_api.main import create_app
from archive_common.audit import AUDIT_LOG
from archive_common.config import get_settings
from archive_common.db import get_engine, get_sessionmaker
from archive_common.models import Game, Vod, VodSegment
from archive_worker import synthetic
from archive_worker.admin import create_admin_app
from archive_worker.vods import splice_reason

A, B = "test-syn-a", "test-syn-b"
AB, P = f"{A}+{B}", "test-syn-p"
KEY = {"Authorization": "Bearer k"}
START = dt.datetime(2001, 3, 4, 20, 0, tzinfo=dt.timezone.utc)  # before any real VOD
OFFSET = 7500
GAME = "test-syn-game"


def _ch(start, length, name="Just Chatting", game_id="509658"):
    return {"gameId": game_id, "name": name, "image": None, "duration": "00:00:00", "start": start, "end": length,
            "restricted": False}


async def _clean():
    async with get_sessionmaker()() as s:
        mine = or_(Vod.id.like("test-syn-%"))
        synthetic_ids = select(Vod.id).where(mine, Vod.synthetic.is_not(None))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.like("vod:test-syn-%")))
        await s.execute(delete(VodSegment).where(VodSegment.vod_id.in_(synthetic_ids)))
        await s.execute(delete(Vod).where(mine, Vod.synthetic.is_not(None)))
        await s.execute(delete(Game).where(Game.vod_id.in_((A, B))))
        await s.execute(delete(Vod).where(mine))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _clean()
    async with get_sessionmaker()() as s:
        s.add_all([
            Vod(id=A, title="big stream", created_at=START, duration="02:00:00", stream_id="2001",
                chapters=[_ch(0, 3600), _ch(3600, 3600, "Test Game", GAME)], thumbnail_url="https://t/a"),
            Vod(id=B, title="big stream ", created_at=START + dt.timedelta(seconds=OFFSET), duration="01:00:00",
                stream_id="2002", chapters=[_ch(0, 3600, "Test Game", GAME)]),
        ])
        await s.flush()
        s.add(Game(vod_id=B, start_time=100, end_time=200, game_name="Test Game", video_id="g1"))
        await s.commit()
    yield
    await _clean()


@pytest.fixture
async def admin(vods, deps, make_service):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        yield c


async def public(path: str) -> httpx.Response:
    """archive-api, a fresh app each time (so nothing is served from its caches)."""
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://t") as c:
        return await c.get(path)


async def _sources() -> dict:
    async with get_sessionmaker()() as s:
        return {v.id: {k: getattr(v, k) for k in ("title", "duration", "chapters", "youtube", "drive", "hidden",
                                                  "thumbnail_url", "merged_into", "created_at", "tags", "synthetic")}
                for v in (await s.execute(select(Vod).where(Vod.id.in_((A, B))))).scalars()}


def _ids(page: dict) -> set[str]:
    return {v["id"] for v in page["data"]}


ALL = "&".join(f"id[$in][]={quote(i)}" for i in (A, B, AB, P, f"{A}-1", f"{A}-2"))


async def test_merge_split_and_undo(admin):
    before = await _sources()

    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})
    assert r.status_code == 201, r.text
    view = r.json()
    assert (view["id"], view["supersedes"], view["tags"], view["duration"]) == (AB, True, [], "03:05:00")
    assert view["segments"] == [{"vod_id": A, "start": 0, "end": None, "at": 0, "label": None},
                                {"vod_id": B, "start": 0, "end": None, "at": OFFSET, "label": None}]
    assert await _sources() == before  # the originals are never written

    vod = (await public(f"/vods/{AB}")).json()
    assert vod["synthetic"] == {"supersedes": True, "segments": [
        {"vodId": A, "start": 0, "end": 7200, "at": 0, "label": None},
        {"vodId": B, "start": 0, "end": 3600, "at": OFFSET, "label": None}]}
    assert (vod["duration"], vod["tags"], vod["createdAt"]) == ("03:05:00", [], "2001-03-04T20:00:00.000Z")
    assert [(c["start"], c.get("kind")) for c in vod["chapters"]] == [(0, None), (3600, None), (7200, "gap"),
                                                                       (OFFSET, None)]
    [game] = vod["games"]
    assert (game["vodId"], game["sourceVodId"], game["start_time"], game["end_time"]) == (AB, B, "7600", "7700")

    original = (await public(f"/vods/{A}")).json()
    assert original["superseded_by"] == [{"id": AB, "start": 0, "end": None, "at": 0}]
    assert "synthetic" not in original and "appears_in" not in original
    assert _ids((await public(f"/vods?{ALL}")).json()) == {AB}
    assert _ids((await public(f"/vods?{ALL}&$superseded=true")).json()) == {A, B, AB}

    page = (await public(f"/v1/vods/{quote(AB)}/comments?content_offset_seconds=0")).json()
    assert page["comments"] == [] and [s["vodId"] for s in page["segments"]] == [A, B]

    # Jobs: the originals are left alone, so they still run; the synthetic VOD is no Twitch VOD.
    assert await splice_reason(A) is None and await splice_reason(B) is None
    assert "synthetic" in await splice_reason(AB)

    # One second of a source goes to one superseding VOD only.
    r = await admin.post(f"/api/v2/vods/{A}/split", headers=KEY, json={"at": 1000})
    assert (r.status_code, r.json()["code"]) == (409, "synthetic_conflict")
    r = await admin.patch(f"/api/v2/vods/{quote(AB)}", headers=KEY, json={"duration": "01:00:00"})
    assert (r.status_code, r.json()["code"]) == (409, "vod_synthetic")
    detail = (await admin.get(f"/api/v2/vods/{A}", headers=KEY)).json()
    assert [x["id"] for x in detail["in_synthetic"]] == [AB]

    r = await admin.delete(f"/api/v2/synthetic/{quote(AB)}", headers=KEY)
    assert r.status_code == 200 and r.json()["id"] == AB
    assert "superseded_by" not in (await public(f"/vods/{A}")).json()
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B}

    # A split, anywhere (no upload boundary needed).
    r = await admin.post(f"/api/v2/vods/{A}/split", headers=KEY, json={"at": 1000.5})
    assert r.status_code == 201, r.text
    assert [(v["id"], v["duration"]) for v in r.json()] == [(f"{A}-1", "00:16:41"), (f"{A}-2", "01:43:20")]
    second = (await public(f"/vods/{A}-2")).json()
    assert second["synthetic"]["segments"] == [{"vodId": A, "start": 1000.5, "end": 7200, "at": 0, "label": None}]
    assert second["createdAt"] == "2001-03-04T20:16:40.500Z"
    assert _ids((await public(f"/vods?{ALL}")).json()) == {f"{A}-1", f"{A}-2", B}
    assert await _sources() == before

    async with get_sessionmaker()() as s:
        audited = (await s.execute(select(AUDIT_LOG.c.action, AUDIT_LOG.c.target)
                                   .where(AUDIT_LOG.c.target.like("vod:test-syn-%")).order_by(AUDIT_LOG.c.id))).all()
    assert [tuple(a) for a in audited] == [("synthetic.create", f"vod:{AB}"), ("synthetic.delete", f"vod:{AB}"),
                                           ("synthetic.create", f"vod:{A}-1"), ("synthetic.create", f"vod:{A}-2")]


async def test_playthrough_tags_and_recompose(admin):
    r = await admin.get(f"/api/v2/playthrough-candidates?game_id={GAME}", headers=KEY)
    assert r.status_code == 200
    windows = r.json()["items"]
    assert [(w["vod_id"], w["start"], w["end"]) for w in windows] == [(A, 3600, 7200), (B, 0, 3600)]

    body = {"id": P, "title": "Test Game, all of it", "tags": ["compilation"],
            "segments": [w["segment"] for w in windows]}
    assert (await admin.post("/api/v2/synthetic", headers=KEY, json={**body, "tags": ["nope"]})).status_code == 422
    r = await admin.post("/api/v2/synthetic", headers=KEY, json=body)
    assert r.status_code == 201, r.text
    assert (r.json()["supersedes"], r.json()["tags"], r.json()["duration"]) == (False, ["compilation"], "02:00:00")
    assert [s["at"] for s in r.json()["segments"]] == [0, 3600]
    assert (await admin.post("/api/v2/synthetic", headers=KEY, json=body)).status_code == 409

    # Untagged lists keep it out; its tab has it; its sources stay listed and link to it.
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B}
    assert _ids((await public(f"/vods?{ALL}&$tag=compilation")).json()) == {P}
    assert _ids((await public(f"/vods?{ALL}&$tag=*")).json()) == {A, B, P}
    assert _ids((await public(f"/vods?{ALL}&tags=compilation")).json()) == {P}
    original = (await public(f"/vods/{B}")).json()
    assert original["appears_in"] == [{"id": P, "title": "Test Game, all of it", "tags": ["compilation"]}]
    assert "superseded_by" not in original
    async with get_engine().connect() as conn:
        [game] = [g for g in await games_played(conn) if g["gameId"] == GAME]
    assert game["vods"] == 2  # A and B, not the playthrough too

    # A source's change reaches it on the next sweep.
    async with get_sessionmaker()() as s:
        await s.execute(update(Vod).where(Vod.id == B).values(
            chapters=[_ch(0, 1800, "Test Game", GAME), _ch(1800, 1800, "Other")], updated_at=func.now()))
        await s.commit()
    assert P in await synthetic.stale_ids(limit=1000)
    assert P in await synthetic.recompose_stale()
    assert P not in await synthetic.stale_ids(limit=1000)
    vod = (await public(f"/vods/{P}?")).json()
    assert [(c["start"], c["name"]) for c in vod["chapters"]] == [(0, "Test Game"), (3600, "Test Game"),
                                                                  (5400, "Other")]

    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"tags": [], "title": "renamed"})
    assert r.status_code == 200 and (r.json()["tags"], r.json()["title"]) == ([], "renamed")
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B, P}
    r = await admin.patch(f"/api/v2/vods/{P}", headers=KEY, json={"tags": ["compilation"], "hidden": True})
    assert r.status_code == 200, r.text
    assert (await public(f"/vods/{P}")).status_code == 404
    assert "appears_in" not in (await public(f"/vods/{B}")).json()  # a hidden VOD is linked from nowhere
