"""Merging, splitting and undoing both through the admin API, against the dev DB.

A (2 h, 5 s missing at its start) and B (1 h, 10 s missing at its start) are one broadcast
that Twitch cut in two: B started 7500 s after A, so there is a 300 s gap between them.
"""

import datetime as dt
import uuid

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, or_, select

from archive_api.main import create_app
from archive_common.db import get_sessionmaker
from archive_common.models import AdminAudit, Emote, Game, Job, Log, Vod, VodSplice
from archive_common.timeutil import hhmmss_to_seconds
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.context import StepRefused
from archive_worker.steps import metadata

A, B, C = "test-splice-a", "test-splice-b", "test-splice-c"
A2 = f"{A}-2"
IDS = (A, B, C, A2)
KEY = {"Authorization": "Bearer k"}
START = dt.datetime(2001, 2, 3, 20, 0, tzinfo=dt.timezone.utc)  # before any real VOD
OFFSET = 7500


def _yt(vid, part, duration, typ="vod"):
    return {"id": vid, "type": typ, "duration": duration, "part": part, "thumbnail_url": f"https://t/{vid}"}


def _ch(start, length, name="Just Chatting", restricted=False):
    return {"gameId": "509658", "name": name, "image": None, "duration": "00:00:00", "start": start, "end": length,
            "restricted": restricted}


def _log(vod_id, offset, name="viewer"):
    return Log(id=uuid.uuid4(), vod_id=vod_id, display_name=name, content_offset_seconds=offset,
               message=[{"text": f"{name} at {offset}"}], user_badges=[], user_color="#fff",
               created_at=START + dt.timedelta(seconds=offset + (OFFSET if vod_id == B else 0)))


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id.in_(IDS)))
        await s.execute(delete(VodSplice).where(or_(VodSplice.vod_id.in_(IDS), VodSplice.other_id.in_(IDS))))
        for model in (Log, Emote, Game):
            await s.execute(delete(model).where(model.vod_id.in_(IDS)))
        await s.execute(delete(Vod).where(Vod.id.in_(IDS)))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _clean()
    async with get_sessionmaker()() as s:
        audit_after = (await s.execute(select(func.max(AdminAudit.id)))).scalar() or 0
        s.add_all([
            Vod(id=A, title="big stream", created_at=START, duration="02:00:00", stream_id="1001",
                chapters=[_ch(0, 3600), _ch(3600, 3600, "Minecraft")],
                youtube=[_yt("a1", 1, 3600), _yt("a2", 2, 3595)], drive=[{"id": "da", "type": "vod"}]),
            Vod(id=B, title="big stream ", created_at=START + dt.timedelta(seconds=OFFSET), duration="01:00:00",
                stream_id="1002", chapters=[_ch(0, 3600, "Minecraft")], youtube=[_yt("b1", 1, 3590)]),
        ])
        await s.flush()
        s.add_all([_log(A, 0), _log(A, 3604), _log(A, 4000), _log(A, 7199), _log(B, 0, "b-viewer"), _log(B, 42, "b-viewer"), _log(B, 3599, "b-viewer")])
        s.add_all([Game(vod_id=B, start_time=100, end_time=200, game_name="Minecraft", video_id="g1"),
                   Emote(vod_id=A, ffz_emotes=[{"id": 1, "code": "a"}], bttv_emotes=[], seventv_emotes=[]),
                   Emote(vod_id=B, ffz_emotes=[{"id": 1, "code": "a"}, {"id": 2, "code": "b"}], bttv_emotes=[],
                         seventv_emotes=[{"id": "x", "code": "EZ"}])])
        await s.commit()
    yield
    await _clean()
    async with get_sessionmaker()() as s:
        await s.execute(delete(AdminAudit).where(AdminAudit.id > audit_after))
        await s.commit()


@pytest.fixture
def admin(deps):
    deps.settings.admin_api_key = SecretStr("k")
    runner = jobs.Runner(deps)
    app = create_admin_app(deps, runner)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin"), runner


@pytest.fixture
def api():
    from archive_common.config import Settings

    app = create_app(Settings(rate_limit_points=1_000_000, cache_ttl_seconds=0))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


def site_delay(vod: dict, typ: str = "vod") -> float:
    """vods-core: duration − Σ part durations − Σ cut lengths."""
    parts = sum(p["duration"] for p in vod["youtube"] if p["type"] == typ)
    cuts = sum(c["end"] for c in vod["chapters"] if c.get("restricted"))
    return hhmmss_to_seconds(vod["duration"]) - parts - cuts


def site_vod_time(vod: dict, upload_time: float, typ: str = "vod") -> float:
    v = upload_time + site_delay(vod, typ)
    for c in sorted((c for c in vod["chapters"] if c.get("restricted")), key=lambda c: c["start"]):
        if c["start"] <= v:
            v += c["end"]
    return v


async def _state() -> dict:
    """Every row these tests touch, minus updatedAt (bumped by any write)."""
    async with get_sessionmaker()() as s:
        vods = {v.id: {k: getattr(v, k) for k in ("title", "duration", "chapters", "youtube", "drive", "stream_id",
                                                  "chapters_locked", "thumbnail_url", "merged_into", "created_at")}
                for v in (await s.execute(select(Vod).where(Vod.id.in_(IDS)))).scalars()}
        logs = sorted((str(r.id), r.vod_id, r.content_offset_seconds, r.seq) for r in
                      (await s.execute(select(Log).where(Log.vod_id.in_(IDS)))).scalars())
        games = sorted((g.id, g.vod_id, g.start_time, g.end_time) for g in
                       (await s.execute(select(Game).where(Game.vod_id.in_(IDS)))).scalars())
        emotes = {e.vod_id: (e.ffz_emotes, e.bttv_emotes, e.seventv_emotes, e.global_emotes) for e in
                  (await s.execute(select(Emote).where(Emote.vod_id.in_(IDS)))).scalars()}
    return {"vods": vods, "logs": logs, "games": games, "emotes": emotes}


async def _comments(vod_id: str) -> dict[str, int]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(Log).where(Log.vod_id == vod_id))).scalars()
        return {r.message[0]["text"]: r.content_offset_seconds for r in rows}


# ── Merge ─────────────────────────────────────────────────────────────────


async def test_merge_keeps_chat_and_video_in_sync(vods, admin, api):
    c, _ = admin
    before_b = await _comments(B)
    async with c:
        r = await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B})
        assert r.status_code == 200, r.text
        body = r.json()
    splice, vod = body["splice"], body["vod"]
    assert (splice["kind"], splice["offset"], splice["gap"]) == ("merge", OFFSET, 300)
    assert splice["detail"]["gapOverridden"] is False and splice["detail"]["movedComments"] == 3
    assert body["warnings"] == []

    # A comment at B's x is now at A's offset + x ...
    after = await _comments(A)
    for text, x in before_b.items():
        assert after[text] == OFFSET + x
    # ... and so is the frame it was shown against: B's first uploaded frame lands at offset + B's delay.
    assert site_delay(vod) == 5  # A's own delay, as before
    a_uploads = 3600 + 3595
    assert site_vod_time(vod, a_uploads) == OFFSET + 10
    assert site_vod_time(vod, a_uploads + 32) == OFFSET + 42  # B's comment at 42 s, B's upload time 32 s

    gap = [ch for ch in vod["chapters"] if ch.get("kind") == "gap"]
    assert [(g["name"], g["start"], g["end"], g["restricted"]) for g in gap] == [
        ("Technical difficulties", 7200, 310, True)]
    assert vod["duration"] == "03:05:00" and vod["chaptersLocked"] is True
    assert [(p["id"], p["part"]) for p in vod["youtube"]] == [("a1", 1), ("a2", 2), ("b1", 3)]
    assert [g["start_time"] for g in vod["games"]] == ["7600"]
    assert [sp["undoable"] for sp in vod["splices"]] == [True]

    async with get_sessionmaker()() as s:
        b = await s.get(Vod, B)
        assert b.merged_into == {"id": A, "offset": OFFSET} and (b.chapters, b.youtube) == ([], [])
        emotes = await s.get(Emote, A)
        assert emotes.ffz_emotes == [{"id": 1, "code": "a"}, {"id": 2, "code": "b"}]
        assert emotes.seventv_emotes == [{"id": "x", "code": "EZ"}]
        audit = (await s.execute(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(1))).scalar_one()
        assert (audit.action, audit.target, audit.detail) == ("POST /admin/vods/{vod_id}/merge", f"vod:{A}",
                                                               {"source": B})

    # What the site sees
    async with api:
        got_b = (await api.get(f"/vods/{B}")).json()
        assert got_b["merged_into"] == {"id": A, "offset": OFFSET}
        assert "merged_into" not in (await api.get(f"/vods/{A}")).json()
        assert (await api.get(f"/vods?id[$in][]={A}&id[$in][]={B}")).json()["total"] == 1
        assert (await api.get(f"/vods?id={B}&$merged=true")).json()["total"] == 1
        assert (await api.get(f"/v1/vods/{B}/comments?content_offset_seconds=0")).json() == {"comments": []}
        page = (await api.get(f"/v1/vods/{A}/comments?content_offset_seconds={OFFSET + 42}")).json()
        assert "b-viewer at 42" in [cm["message"][0]["text"] for cm in page["comments"]]
        emotes = (await api.get(f"/emotes?vod_id={A}")).json()["data"][0]
        assert [e["id"] for e in emotes["ffz_emotes"]] == [1, 2]
        played = {g["gameId"]: g for g in (await api.get("/v1/games-played")).json()}
        assert all(g["name"] != "Technical difficulties" for g in played.values())


async def test_merge_then_unmerge_restores_both_exactly(vods, admin):
    c, _ = admin
    before = await _state()
    async with c:
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B, "gap": 120})).status_code == 200
        async with get_sessionmaker()() as s:
            sp = (await s.execute(select(VodSplice).where(VodSplice.vod_id == A))).scalar_one()
            assert (sp.detail["gapOverridden"], sp.detail["computedGap"], sp.detail["offset"]) == (True, 300, 7320)
        assert (await _state()) != before
        r = await c.post(f"/admin/vods/{A}/unmerge", headers=KEY, json={"source": B})
        assert r.status_code == 200, r.text
        assert r.json()["splice"]["undoneAt"] and r.json()["vod"]["splices"] == []
        assert (await c.post(f"/admin/vods/{A}/unmerge", headers=KEY, json={"source": B})).status_code == 404
    assert (await _state()) == before


async def test_unmerge_refuses_to_lose_edits_unless_forced(vods, admin):
    c, _ = admin
    async with c:
        await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B})
        await c.patch(f"/admin/vods/{A}", headers=KEY, json={"title": "renamed"})
        r = await c.post(f"/admin/vods/{A}/unmerge", headers=KEY, json={"source": B})
        assert r.status_code == 409 and r.json()["edited"] == [f"{A}.title"]
        r = await c.post(f"/admin/vods/{A}/unmerge", headers=KEY, json={"source": B, "force": True})
        assert r.status_code == 200 and r.json()["vod"]["title"] == "big stream"


async def test_merge_refusals(vods, admin):
    c, runner = admin
    async with c:
        # Wrong order: B is the later VOD.
        r = await c.post(f"/admin/vods/{B}/merge", headers=KEY, json={"source": A})
        assert r.status_code == 409 and "started before" in r.json()["msg"]
        # Overlapping: a gap override that puts B inside A.
        async with get_sessionmaker()() as s:
            (await s.get(Vod, B)).created_at = START + dt.timedelta(seconds=7000)
            await s.commit()
        r = await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B})
        assert r.status_code == 409 and "overlap" in r.json()["msg"]
        assert (r.json()["offset"], r.json()["targetDuration"], r.json()["gap"]) == (7000, 7200, -200)
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B, "gap": -5})).status_code == 400
        # A running (or queued, or paused) job on either VOD.
        job = await jobs.enqueue("chat", B, paused=True, settings=runner.deps.settings)
        r = await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B, "gap": 300})
        assert r.status_code == 409 and r.json()["jobs"] == [job.id]
        await runner.cancel(job.id)
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B, "gap": 300})).status_code == 200
        # An already-merged source (or target).
        async with get_sessionmaker()() as s:
            s.add(Vod(id=C, title="c", created_at=START + dt.timedelta(hours=5), duration="00:10:00"))
            await s.commit()
        r = await c.post(f"/admin/vods/{C}/merge", headers=KEY, json={"source": B})
        assert r.status_code == 409 and "already merged into" in r.json()["msg"]
        r = await c.post(f"/admin/vods/{B}/merge", headers=KEY, json={"source": C})
        assert r.status_code == 409 and "already merged into" in r.json()["msg"]
        # Different upload types.
        r = await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": C})
        assert r.status_code == 409 and "same types" in r.json()["msg"]
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": A})).status_code == 400
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": "nope"})).status_code == 404


async def test_refresh_jobs_refuse_a_merged_vod(vods, admin, make_ctx, monkeypatch):
    c, runner = admin
    async with c:
        assert (await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B})).status_code == 200
        for route, body in (("/admin/logs", {"vodId": A}), ("/admin/chapters", {"vodId": A, "force": True}),
                            ("/admin/emotes", {"vodId": A}), ("/admin/duration", {"vodId": A}),
                            ("/admin/download", {"vodId": A}), ("/admin/logs", {"vodId": B}),
                            ("/admin/jobs", {"kind": "chat", "vodId": A})):
            r = await c.post(route, headers=KEY, json=body)
            assert r.status_code == 409, route
            assert "merged" in r.json()["msg"]
        r = await c.request("DELETE", "/admin/delete", headers=KEY, json={"vodId": A})
        assert r.status_code == 409

    async def boom(*_a, **_k):
        raise AssertionError("fetched from Twitch")

    monkeypatch.setattr(runner.deps.gql, "comments", boom)
    monkeypatch.setattr(runner.deps.gql, "video_moments", boom)
    comments = await _comments(A)
    for step, payload in ((metadata.chat, {}), (metadata.chapters, {"force": True}), (metadata.emotes, {})):
        with pytest.raises(StepRefused, match=f"vod {A} was merged with {B}"):
            await step(make_ctx("chat", A, payload))
    with pytest.raises(StepRefused, match=f"vod {B} was merged into {A}"):
        await metadata.chat(make_ctx("chat", B))
    assert await _comments(A) == comments and len(comments) == 7  # B's rows are still there

    # A job queued before the merge fails at once, without retries.
    job = await jobs.enqueue("chat", A, settings=runner.deps.settings)
    await runner._run(await runner._claim())
    job = await jobs.get(job.id)
    assert (job.state, job.attempts) == ("failed", jobs.MAX_ATTEMPTS)
    assert "refused" in job.last_error


# ── Split ─────────────────────────────────────────────────────────────────


async def test_split_at_a_part_boundary_and_unsplit(vods, admin):
    c, _ = admin
    before = await _state()
    async with c:
        r = await c.post(f"/admin/vods/{A}/split", headers=KEY, json={"at": 3000})
        assert r.status_code == 409 and r.json()["validPoints"] == [{"at": 3605, "from": 3605, "to": 3605}]

        r = await c.post(f"/admin/vods/{A}/split", headers=KEY, json={"at": 3605})
        assert r.status_code == 200, r.text
        assert r.json()["newVodId"] == A2
        first = r.json()["vod"]
        second = (await c.get(f"/admin/vods/{A2}", headers=KEY)).json()
        assert (first["duration"], second["duration"]) == ("01:00:05", "00:59:55")
        assert second["createdAt"] == "2001-02-03T21:00:05.000Z"
        assert site_delay(first) == 5 and site_delay(second) == 0
        assert [(p["id"], p["part"]) for p in first["youtube"]] == [("a1", 1)]
        assert [(p["id"], p["part"]) for p in second["youtube"]] == [("a2", 1)]
        # The comment at A's 4000 s was at upload time 3995 (part 2, 395 s in); it still is.
        assert (await _comments(A2))["viewer at 4000"] == 395 and site_vod_time(second, 395) == 395
        assert (await _comments(A))["viewer at 3604"] == 3604
        assert [sp["kind"] for sp in second["splices"]] == ["split"]
        async with get_sessionmaker()() as s:
            assert (await s.get(Emote, A2)).ffz_emotes == [{"id": 1, "code": "a"}]

        r = await c.post(f"/admin/vods/{A}/unsplit", headers=KEY, json={})
        assert r.status_code == 200, r.text
    assert (await _state()) == before


async def test_split_at_the_join_undoes_the_merge(vods, admin):
    c, _ = admin
    before = await _state()
    async with c:
        await c.post(f"/admin/vods/{A}/merge", headers=KEY, json={"source": B})
        # Elsewhere, a real split; the merge cannot be undone until that is.
        r = await c.post(f"/admin/vods/{A}/split", headers=KEY, json={"at": 3605})
        assert r.status_code == 200 and r.json()["newVodId"] == A2
        r = await c.post(f"/admin/vods/{A}/unmerge", headers=KEY, json={"source": B})
        assert r.status_code == 409 and "undo that first" in r.json()["msg"]
        async with get_sessionmaker()() as s:
            assert (await s.get(Vod, B)).merged_into == {"id": A2, "offset": OFFSET - 3605}  # still one hop
        assert (await c.post(f"/admin/vods/{A}/unsplit", headers=KEY, json={"source": A2})).status_code == 200

        r = await c.post(f"/admin/vods/{A}/split", headers=KEY, json={"at": 7300})  # inside the gap chapter
        assert r.status_code == 200, r.text
        assert r.json()["undid"] == "merge"
    assert (await _state()) == before


# ── Candidates ────────────────────────────────────────────────────────────


async def test_merge_candidates(vods, admin):
    c, _ = admin
    async with c:
        got = (await c.get(f"/admin/vods/{A}/merge-candidates", headers=KEY)).json()
        assert got["withinMinutes"] == 30 and got["vod"]["endsAt"] == "2001-02-03T22:00:00+00:00"
        assert got["candidates"] == [{"id": B, "streamId": "1002", "title": "big stream ", "duration": "01:00:00",
                                      "createdAt": "2001-02-03T22:05:00+00:00", "gap": 300, "overlaps": False,
                                      "titlesMatch": True}]
        assert (await c.get(f"/admin/vods/{B}/merge-candidates", headers=KEY)).json()["candidates"] == []
        assert (await c.get("/admin/vods/nope/merge-candidates", headers=KEY)).status_code == 404
