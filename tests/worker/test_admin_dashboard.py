"""Admin endpoints for the web dashboard: job events, job edits, VOD edits, health, audit (needs the dev DB)."""

import asyncio
import datetime as dt

import asyncpg
import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update

from archive_common.config import get_settings
from archive_common.db import VOD_CHANGED, execute, get_sessionmaker
from archive_common.models import AdminAudit, Emote, Game, Job, Stream, Vod
from archive_common.twitch.helix import HELIX, TOKEN_URL
from archive_api.invalidation import asyncpg_dsn
from archive_worker import events as events_mod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import SESSION_COOKIE
from archive_worker.events import JobEvents
from archive_worker.steps import metadata

VOD = "test-admin-dashboard-vod"
STREAM = 999_000_000_001
KEY = {"Authorization": "Bearer k"}
TEMPLATE = "https://static-cdn.jtvnw.net/ttv-boxart/509658-{width}x{height}.jpg"


async def _reset(audit_after: int | None = None):
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id == VOD))  # job_events cascade
        await s.execute(delete(Emote).where(Emote.vod_id == VOD))
        await s.execute(delete(Game).where(Game.vod_id == VOD))
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.execute(delete(Stream).where(Stream.id == STREAM))
        if audit_after is not None:
            await s.execute(delete(AdminAudit).where(AdminAudit.id > audit_after))
        await s.commit()


@pytest.fixture
async def vod(db):
    await _reset()
    async with get_sessionmaker()() as s:
        audit_after = (await s.execute(select(func.max(AdminAudit.id)))).scalar() or 0
    async with get_sessionmaker()() as s:
        s.add(Vod(id=VOD, title="old title", created_at=dt.datetime.now(dt.timezone.utc), duration="02:00:00",
                  youtube=[{"id": "yt1", "type": "vod", "duration": 7200, "part": 1, "thumbnail_url": "https://t/1"}]))
        await s.commit()
    yield VOD
    await _reset(audit_after)


@pytest.fixture
def steps(monkeypatch):
    """Kind "test": step a logs and reports progress, step b warns."""

    async def a(ctx):
        ctx.log.info("hello from a")
        ctx.progress(1, 2, "parts", "half way")

    async def b(ctx):
        ctx.log.warning("careful in b")

    monkeypatch.setitem(jobs.KINDS, "test", ["a", "b"])
    monkeypatch.setitem(jobs.STEPS, "a", a)
    monkeypatch.setitem(jobs.STEPS, "b", b)


@pytest.fixture
def recorder(deps):
    rec = JobEvents()
    rec.install()
    deps.events = rec
    yield rec
    rec.uninstall()


@pytest.fixture
def app(deps, recorder):
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.admin_password = SecretStr("pw")
    runner = jobs.Runner(deps)
    return create_admin_app(deps, runner), runner


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app[0]), base_url="https://admin")


# ── Job events ────────────────────────────────────────────────────────────


async def test_job_events_record_logs_steps_and_progress(vod, steps, app):
    runner = app[1]
    job = await jobs.enqueue("test", vod, settings=runner.deps.settings)
    claimed = await runner._claim()
    assert claimed.id == job.id
    await runner._run(claimed)

    async with client(app) as c:
        page = (await c.get(f"/admin/jobs/{job.id}/events", headers=KEY)).json()
        rows = page["data"]
        assert [(e["level"], e["step"], e["message"]) for e in rows] == [
            ("info", "a", "running test (vod=test-admin-dashboard-vod) from step a"),
            ("info", "a", "step a"),
            ("info", "a", "hello from a"),
            ("info", "a", "half way"),
            ("info", "b", "step b"),
            ("warning", "b", "careful in b"),
            ("info", None, "job test finished"),
        ]
        assert rows[3]["progress"] == {"done": 1, "total": 2, "unit": "parts"}
        assert rows[2]["progress"] is None
        assert all(e["at"].endswith("+00:00") for e in rows)
        seqs = [e["seq"] for e in rows]
        assert seqs == sorted(seqs) and page["next"] == seqs[-1]

        newer = (await c.get(f"/admin/jobs/{job.id}/events?after={seqs[2]}&limit=2", headers=KEY)).json()
        assert [e["seq"] for e in newer["data"]] == seqs[3:5] and newer["next"] == seqs[4]
        empty = (await c.get(f"/admin/jobs/{job.id}/events?after={seqs[-1]}", headers=KEY)).json()
        assert empty == {"data": [], "next": seqs[-1]}
        assert (await c.get("/admin/jobs/0/events", headers=KEY)).status_code == 404


async def test_job_events_are_capped_per_job(vod, monkeypatch, deps):
    monkeypatch.setattr(events_mod, "PRUNE_EVERY", 1)
    job = await jobs.enqueue("chat", vod, paused=True, settings=deps.settings)
    rec = JobEvents(cap=5)
    for i in range(12):
        rec.add(job.id, "info", "chat", f"line {i}")
    await rec.flush()
    rec.add(job.id, "info", "chat", "line 12")
    assert [e.message for e in await rec.list(job.id)] == [f"line {i}" for i in range(8, 13)]


# ── Job list paging and PATCH ─────────────────────────────────────────────


async def test_jobs_before_paging_and_patch(vod, app):
    settings = app[1].deps.settings
    ids = [(await jobs.enqueue("download", vod, paused=True, settings=settings)).id for _ in range(3)]
    async with client(app) as c:
        page = (await c.get(f"/admin/jobs?vodId={vod}&limit=2", headers=KEY)).json()["data"]
        assert [j["id"] for j in page] == ids[:0:-1]
        older = (await c.get(f"/admin/jobs?vodId={vod}&before={page[-1]['id']}", headers=KEY)).json()["data"]
        assert [j["id"] for j in older] == [ids[0]]

        r = await c.patch(f"/admin/jobs/{ids[0]}", headers=KEY, json={"pauseBefore": ["upload"], "pauseNext": True})
        assert r.status_code == 200
        assert (r.json()["id"], r.json()["pauseBefore"], r.json()["pauseNext"]) == (ids[0], ["upload"], True)
        r = await c.patch(f"/admin/jobs/{ids[0]}", headers=KEY, json={"pauseBefore": None})
        assert r.json()["pauseBefore"] is None and r.json()["pauseNext"] is True

        bad_step = await c.patch(f"/admin/jobs/{ids[0]}", headers=KEY, json={"pauseBefore": ["capture"]})
        assert bad_step.status_code == 400 and "no step(s) capture" in bad_step.json()["msg"]
        for body in ({"pauseNext": "yes"}, {"pauseBefore": "upload"}, {"state": "done"}, {}):
            assert (await c.patch(f"/admin/jobs/{ids[0]}", headers=KEY, json=body)).status_code == 400
        assert (await c.patch("/admin/jobs/0", headers=KEY, json={"pauseNext": True})).status_code == 404


# ── VOD editing ───────────────────────────────────────────────────────────


def chapter(start, length, name="Just Chatting"):
    return {"name": name, "gameId": "509658", "imageTemplate": TEMPLATE, "start": start, "length": length,
            "restricted": False}


async def test_vod_editing(vod, app, make_ctx, monkeypatch):
    await jobs.enqueue("emotes", vod, paused=True, settings=app[1].deps.settings)
    async with client(app) as c:
        got = (await c.get(f"/admin/vods/{vod}", headers=KEY)).json()
        assert (got["id"], got["title"], got["chaptersLocked"], got["games"]) == (vod, "old title", False, [])
        assert got["duration_seconds"] == 7200  # the public API's own fields
        assert [j["kind"] for j in got["jobs"]] == ["emotes"]
        assert (await c.get("/admin/vods/nope", headers=KEY)).status_code == 404

        r = await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"title": "  new title "})
        assert r.status_code == 200 and r.json()["title"] == "new title"
        assert (await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"title": ""})).status_code == 400
        assert (await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"duration": "1"})).status_code == 400

        chapters = [chapter(0, 3600), chapter(3600, 3600, "Artifact")]
        r = await c.put(f"/admin/vods/{vod}/chapters", headers=KEY, json={"chapters": chapters, "locked": True})
        assert r.status_code == 200
        saved = r.json()
        assert saved["chaptersLocked"] is True
        assert [(ch["name"], ch["duration"], ch["start"], ch["end"], ch["length"]) for ch in saved["chapters"]] == [
            ("Just Chatting", "00:00:00", 0, 3600, 3600), ("Artifact", "01:00:00", 3600, 3600, 3600),
        ]
        assert saved["chapters"][0]["image"].endswith("-40x53.jpg")
        assert saved["chapters"][0]["imageTemplate"] == TEMPLATE

        too_long = await c.put(f"/admin/vods/{vod}/chapters", headers=KEY,
                               json={"chapters": [chapter(0, 7300)], "locked": False})
        assert too_long.status_code == 400 and "after the end of the VOD" in too_long.json()["msg"]
        no_lock = await c.put(f"/admin/vods/{vod}/chapters", headers=KEY, json={"chapters": []})
        assert no_lock.status_code == 400

        r = await c.put(f"/admin/vods/{vod}/youtube", headers=KEY,
                        json={"youtube": [{"id": "yt1", "type": "vod", "part": 1},
                                          {"id": "yt2", "type": "live", "part": 1, "duration": 7100}]})
        assert r.json()["youtube"] == [
            {"id": "yt1", "type": "vod", "duration": 7200, "part": 1, "thumbnail_url": "https://t/1"},
            {"id": "yt2", "type": "live", "duration": 7100, "part": 1,
             "thumbnail_url": "https://i.ytimg.com/vi/yt2/mqdefault.jpg"},
        ]
        bad = await c.put(f"/admin/vods/{vod}/youtube", headers=KEY, json={"youtube": [{"id": "x", "type": "?"}]})
        assert bad.status_code == 400

        r = await c.put(f"/admin/vods/{vod}/drive", headers=KEY, json={"drive": [{"id": "d1", "type": "live"}]})
        assert r.json()["drive"] == [{"id": "d1", "type": "live"}]

        assert (await c.get(f"/admin/vods/{vod}/emotes", headers=KEY)).json() is None
        async with get_sessionmaker()() as s:
            s.add(Emote(vod_id=vod, ffz_emotes=[{"id": 1, "code": "x"}]))
            await s.commit()
        emotes = (await c.get(f"/admin/vods/{vod}/emotes", headers=KEY)).json()
        assert emotes["vodId"] == vod and emotes["ffz_emotes"] == [{"id": 1, "code": "x"}]

    # The automatic chapters step leaves locked chapters alone unless forced.
    calls = []

    async def moments(vod_id):
        calls.append(vod_id)
        return None  # "no chapter data": keeps the chapters either way

    monkeypatch.setattr(app[1].deps.gql, "video_moments", moments)
    await metadata.chapters(make_ctx("chapters", vod))
    assert calls == []
    await metadata.chapters(make_ctx("chapters", vod, {"force": True}))
    assert calls == [vod]


async def _listen() -> tuple[asyncpg.Connection, asyncio.Queue[str]]:
    conn = await asyncpg.connect(asyncpg_dsn(get_settings().database_url))
    heard: asyncio.Queue[str] = asyncio.Queue()
    await conn.add_listener(VOD_CHANGED, lambda _c, _pid, _ch, payload: heard.put_nowait(payload))
    return conn, heard


async def test_vod_edit_notifies_the_api(vod, app):
    conn, heard = await _listen()
    try:
        async with client(app) as c:
            assert (await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"title": "x"})).status_code == 200
        assert await asyncio.wait_for(heard.get(), 5) == vod
    finally:
        await conn.close()


async def test_any_vod_or_game_write_notifies_the_api(vod):
    """The trigger covers writers outside the admin API (job steps, the monitor)."""
    conn, heard = await _listen()
    try:
        await execute(update(Vod).where(Vod.id == vod).values(duration="03:00:00"))
        assert await asyncio.wait_for(heard.get(), 5) == vod
        await execute(update(Vod).where(Vod.id == vod).values(duration="03:00:00"))  # no change: silent
        async with get_sessionmaker()() as s:
            s.add(Game(vod_id=vod, game_name="Just Chatting"))
            await s.commit()
        assert await asyncio.wait_for(heard.get(), 5) == vod
        await asyncio.sleep(0.2)
        assert heard.empty()
    finally:
        await conn.close()


@respx.mock
async def test_twitch_game_search(app, respx_mock):
    settings = app[1].deps.settings
    settings.twitch_client_id, settings.twitch_client_secret = "cid", SecretStr("secret")
    respx_mock.post(TOKEN_URL).respond(json={"access_token": "t", "expires_in": 3600})
    search = respx_mock.get(f"{HELIX}/search/categories").respond(json={"data": [
        {"id": "509658", "name": "Just Chatting",
         "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/509658-52x72.jpg"},
    ]})
    async with client(app) as c:
        r = await c.get("/admin/twitch/games?query=just", headers=KEY)
        assert r.json() == [{"gameId": "509658", "name": "Just Chatting", "imageTemplate": TEMPLATE}]
        assert search.calls.last.request.url.params["query"] == "just"
        assert (await c.get("/admin/twitch/games?query=", headers=KEY)).status_code == 400


# ── Health ────────────────────────────────────────────────────────────────


@respx.mock
async def test_health(vod, app, monkeypatch, respx_mock):
    deps = app[1].deps
    deps.settings.api_internal_url = "http://api.test"
    respx_mock.get("http://api.test/healthz").respond(json={"ok": True})
    checked = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)

    async def cached_check(max_age):
        assert max_age == 600
        return {"authorized": True, "valid": False, "error": "RefreshError: invalid_grant", "checkedAt": checked}

    monkeypatch.setattr(deps.youtube, "cached_check", cached_check)
    job = await jobs.enqueue("chat", vod, settings=deps.settings)
    async with get_sessionmaker()() as s:
        (await s.get(Job, job.id)).state = "failed"
        s.add(Stream(id=STREAM, started_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365), is_live=True))
        await s.commit()

    async with client(app) as c:
        h = (await c.get("/admin/health", headers=KEY)).json()
    assert h["worker"]["ok"] is True and h["worker"]["runningJobs"] == 0 and h["worker"]["startedAt"]
    assert h["api"] == {"ok": True}
    assert h["youtube"] == {"authorized": True, "valid": False, "error": "RefreshError: invalid_grant",
                            "checkedAt": "2026-09-25T12:00:00+00:00"}
    assert h["live"]["live"] is True and h["live"]["streamId"] == str(STREAM) and h["live"]["startedAt"]
    assert set(h["jobs"]["counts"]) == set(jobs.STATES) and h["jobs"]["counts"]["failed"] >= 1
    assert h["jobs"]["recentFailures"][0]["id"] == job.id and len(h["jobs"]["recentFailures"]) <= 5

    respx_mock.get("http://api.test/healthz").mock(side_effect=httpx.ConnectError("down"))
    async with client(app) as c:
        assert (await c.get("/admin/health", headers=KEY)).json()["api"] == {"ok": False}


# ── Audit log ─────────────────────────────────────────────────────────────


async def test_audit_log_records_state_changes_with_actor(vod, app):
    async with client(app) as c:
        await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"title": "by key"})
        session = (await c.post("/admin/session", json={"password": "pw"})).json()
        assert c.cookies.get(SESSION_COOKIE)
        await c.put(f"/admin/vods/{vod}/drive", headers={"X-CSRF-Token": session["csrf"]}, json={"drive": []})
        await c.patch(f"/admin/vods/{vod}", headers=KEY, json={"title": ""})  # 400: nothing changed
        await c.get(f"/admin/vods/{vod}", headers=KEY)  # reads are not audited

        mine = [e for e in (await c.get("/admin/audit?limit=500", headers=KEY)).json()["data"]
                if e["target"] == f"vod:{vod}"]
        assert [(e["actor"], e["action"], e["detail"]) for e in mine] == [
            ("password", "PUT /admin/vods/{vod_id}/drive", {"drive": []}),
            ("api-key", "PATCH /admin/vods/{vod_id}", {"title": "by key"}),
        ]
        assert mine[0]["at"].endswith("+00:00")
        older = (await c.get(f"/admin/audit?before={mine[0]['id']}&limit=1", headers=KEY)).json()["data"]
        assert [e["action"] for e in older] == ["POST /admin/session"]  # the login, between the two

    async with get_sessionmaker()() as s:
        logins = (await s.execute(
            select(AdminAudit).where(AdminAudit.action == "POST /admin/session").order_by(AdminAudit.id.desc())
        )).scalars().first()
    assert logins is not None and logins.actor == "password" and logins.detail == {}  # password never stored
