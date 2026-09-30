"""Dashboard sessions in the database (admin_sessions): they outlast the worker, and only a hash is stored."""

import httpx
from archive_common.db import get_sessionmaker
from archive_common.models import AdminSession
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import AdminAuth, DbSessionStore, token_hash
from pydantic import SecretStr
from sqlalchemy import delete, select

from test_admin_signin import ALICE, FakeAuth, sign_in


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def rows() -> list[AdminSession]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(AdminSession))).scalars())


async def test_sessions_outlast_a_restart_and_only_the_hash_is_kept(db):
    async with get_sessionmaker()() as s:
        await s.execute(delete(AdminSession))
        await s.commit()
    clock = Clock()
    first = AdminAuth("pw", ttl_s=60, clock=clock, store=DbSessionStore())
    session = await first.login("twitch:100", ALICE, "sid-1")
    [row] = await rows()
    assert row.token_hash == token_hash(session.token) != session.token
    assert row.twitch_user == ALICE and row.sid == "sid-1"

    restarted = AdminAuth("pw", ttl_s=60, clock=clock, store=DbSessionStore())
    found = await restarted.session(session.token)
    assert found == session  # everything, to the second: csrf, actor, user, sid, times

    clock.now += 30
    await restarted.checked(found, clock.now)
    assert (await restarted.session(session.token)).checked_at == clock.now

    clock.now += 30  # expired: gone on sight
    assert await restarted.session(session.token) is None
    assert await rows() == []

    stale = await restarted.login()
    clock.now += 60
    fresh = await restarted.login()  # a login sweeps expired rows
    assert [r.token_hash for r in await rows()] == [token_hash(fresh.token)]
    await restarted.logout(fresh.token)
    assert await rows() == [] and await restarted.session(stale.token) is None


async def test_audit_names_the_twitch_login(db, deps):
    fake = FakeAuth()
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.admin_twitch_ids = ["100"]

    def client(cookies: httpx.Cookies | None = None) -> httpx.AsyncClient:
        app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)), signin=fake, sessions=DbSessionStore())
        transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 1234))
        return httpx.AsyncClient(transport=transport, base_url="https://admin", cookies=cookies)

    async with client() as first:
        await sign_in(first, fake)
    # A new app over the same database (a restarted worker): the session is still there.
    async with client(first.cookies) as c:
        session = (await c.get("/admin/session")).json()
        assert session["authenticated"] and session["user"] == ALICE
        assert (await c.delete("/admin/session", headers={"X-CSRF-Token": session["csrf"]})).status_code == 204

        audit = (await c.get("/admin/audit?limit=5", headers={"Authorization": "Bearer k"})).json()["data"]
    out, signed_in = audit[0], audit[1]
    assert (out["action"], out["actor"], out["actorLogin"]) == ("DELETE /admin/session", "twitch:100", "alice")
    assert (signed_in["action"], signed_in["actorLogin"]) == ("GET /admin/signin/callback", "alice")
