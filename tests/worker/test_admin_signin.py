"""The admin password is local-only; Twitch sign-in through vexoulz-auth (faked here) works from anywhere."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import SESSION_COOKIE
from archive_worker.admin_signin import (
    STATE_COOKIE,
    PendingStates,
    SignedIn,
    SignInError,
    VexoulzAuth,
    safe_next,
)
from pydantic import SecretStr

ALICE = {"id": "100", "login": "alice", "displayName": "Alice", "avatar": None, "color": None}
MALLORY = {"id": "666", "login": "mallory", "displayName": "Mallory", "avatar": None, "color": None}


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAuth:
    """vexoulz-auth as the worker sees it: codes to users, and which sessions are still signed in."""

    def __init__(self) -> None:
        self.codes: dict[str, SignedIn] = {}
        self.signed_out: set[str] = set()
        self.down = False
        self.checks = 0

    def authorize_url(self, state: str) -> str:
        return f"https://auth.test/authorize?client_id=vods-admin&state={state}"

    def approve(self, user: dict, sid: str = "sid-1") -> str:
        code = f"code-{len(self.codes)}"
        self.codes[code] = SignedIn(user, sid)
        return code

    async def redeem(self, code: str) -> SignedIn:
        if self.down:
            raise SignInError("down")
        try:
            return self.codes.pop(code)
        except KeyError:
            raise SignInError("refused") from None

    async def active(self, sid: str) -> bool:
        self.checks += 1
        if self.down:
            raise SignInError("down")
        return sid not in self.signed_out


@pytest.fixture
def fake() -> FakeAuth:
    return FakeAuth()


@pytest.fixture
def admin(deps, fake):
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.admin_password = SecretStr("correct horse")
    deps.settings.admin_twitch_ids = ["100"]

    def client(peer: str = "203.0.113.5", *, networks: list[str] | None = None, signin=fake) -> httpx.AsyncClient:
        if networks is not None:
            deps.settings.admin_password_networks = networks
        app = create_admin_app(deps, jobs.Runner(deps), signin=signin)
        transport = httpx.ASGITransport(app=app, client=(peer, 1234))
        return httpx.AsyncClient(transport=transport, base_url="https://admin")

    return client


async def sign_in(c: httpx.AsyncClient, fake: FakeAuth, user: dict = ALICE, next: str = "/admin/jobs",
                  sid: str = "sid-1") -> httpx.Response:
    start = await c.get("/admin/signin", params={"next": next})
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await c.get("/admin/signin/callback", params={"code": fake.approve(user, sid), "state": state})


def error_of(r: httpx.Response) -> str:
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert url.path == "/admin/login"
    return parse_qs(url.query)["auth_error"][0]


# ── Password: local network only ─────────────────────────────────────────


async def test_password_is_refused_outside_the_local_network(admin):
    async with admin("203.0.113.5") as c:
        assert (await c.get("/admin/session")).json()["passwordLogin"] is False
        r = await c.post("/admin/session", json={"password": "correct horse"})
        assert r.status_code == 403 and "local network" in r.json()["msg"]
    async with admin("192.168.1.20") as c:
        assert (await c.get("/admin/session")).json()["passwordLogin"] is True
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200
    async with admin("::1") as c:
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200


async def test_password_networks_can_be_opened_or_narrowed(admin):
    async with admin("203.0.113.5", networks=["*"]) as c:
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200
    async with admin("192.168.1.20", networks=["10.0.0.0/8"]) as c:
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 403


async def test_password_behind_a_proxy_counts_the_real_client(admin, deps):
    deps.settings.admin_trusted_proxies = ["127.0.0.1"]
    async with admin("127.0.0.1") as c:
        outside = {"X-Forwarded-For": "203.0.113.9"}
        assert (await c.post("/admin/session", json={"password": "correct horse"}, headers=outside)).status_code == 403
        inside = {"X-Forwarded-For": "192.168.1.9"}
        assert (await c.post("/admin/session", json={"password": "correct horse"}, headers=inside)).status_code == 200


# ── Twitch sign-in ───────────────────────────────────────────────────────


async def test_twitch_sign_in_from_anywhere(admin, fake):
    async with admin("203.0.113.5") as c:
        before = (await c.get("/admin/session")).json()
        assert before["twitchLogin"] is True and before["passwordLogin"] is False

        start = await c.get("/admin/signin", params={"next": "/admin/vods/1"})
        assert start.headers["location"].startswith("https://auth.test/authorize?")
        state_cookie = start.headers["set-cookie"].lower()
        for attr in ("httponly", "secure", "samesite=lax", "path=/"):
            assert attr in state_cookie

        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        done = await c.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": state})
        assert done.status_code == 302 and done.headers["location"] == "/admin/vods/1"
        assert SESSION_COOKIE in done.cookies and "samesite=strict" in str(done.headers).lower()

        session = (await c.get("/admin/session")).json()
        assert session["authenticated"] and session["user"] == ALICE
        assert (await c.get("/admin/kinds")).status_code == 200
        bad = await c.patch("/admin/jobs/1", json={"pauseNext": True})
        assert bad.status_code == 403  # CSRF still applies

        out = await c.delete("/admin/session", headers={"X-CSRF-Token": session["csrf"]})
        assert out.status_code == 204
        assert (await c.get("/admin/kinds")).status_code == 403


async def test_only_listed_twitch_users_get_in(admin, fake):
    async with admin() as c:
        assert error_of(await sign_in(c, fake, MALLORY)) == "not_allowed"
        assert (await c.get("/admin/session")).json()["authenticated"] is False


async def test_state_must_be_ours_and_used_once(admin, fake):
    async with admin() as c:
        forged = await c.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": "made-up"})
        assert error_of(forged) == "expired"

        start = await c.get("/admin/signin")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        assert (await c.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": state})).status_code == 302
        again = await c.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": state})
        assert error_of(again) == "expired"

    # A state started in another browser (no matching cookie) is refused: no login CSRF.
    async with admin() as attacker, admin() as victim:
        start = await attacker.get("/admin/signin")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        r = await victim.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": state})
        assert error_of(r) == "expired"


async def test_errors_from_vexoulz_auth_reach_the_login_page(admin, fake):
    async with admin() as c:
        start = await c.get("/admin/signin")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        r = await c.get("/admin/signin/callback", params={"error": "denied", "state": state})
        assert error_of(r) == "denied"
        assert parse_qs(urlsplit(r.headers["location"]).query)["next"] == ["/admin"]

        start = await c.get("/admin/signin")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        assert error_of(await c.get("/admin/signin/callback", params={"error": "<odd>", "state": state})) == "twitch"

        fake.down = True
        assert error_of(await sign_in(c, fake)) == "unavailable"


async def test_sign_out_everywhere_is_noticed_after_check_interval(deps, fake):
    from archive_worker.admin_signin import CHECK_S

    deps.settings.admin_twitch_ids = ["100"]
    app = create_admin_app(deps, jobs.Runner(deps), signin=fake)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 1234))
    clock = Clock()
    app.state.admin_sessions.clock = clock
    async with httpx.AsyncClient(transport=transport, base_url="https://admin") as c:
        await sign_in(c, fake, sid="sid-9")
        assert (await c.get("/admin/kinds")).status_code == 200
        assert fake.checks == 0  # just signed in: nothing to ask yet

        fake.down = True
        clock.now += CHECK_S
        assert (await c.get("/admin/kinds")).status_code == 200  # can't tell: the session stands
        assert fake.checks == 1

        fake.down = False
        fake.signed_out.add("sid-9")
        assert (await c.get("/admin/kinds")).status_code == 403
        assert (await c.get("/admin/session")).json()["authenticated"] is False


async def test_twitch_sign_in_off_without_settings(deps):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, jobs.Runner(deps))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.get("/admin/session")).json()["twitchLogin"] is False
        assert (await c.get("/admin/signin")).status_code == 404


def test_client_is_built_only_when_fully_configured(settings):
    assert VexoulzAuth.from_settings(settings) is None
    settings.admin_auth_url = "https://auth.test/"
    settings.admin_auth_client_secret = SecretStr("s")
    settings.admin_auth_redirect_url = "https://vods.test/backend-admin/admin/signin/callback"
    assert VexoulzAuth.from_settings(settings) is None  # nobody is allowed in yet
    settings.admin_twitch_ids = ["100"]
    client = VexoulzAuth.from_settings(settings)
    assert client is not None
    url = urlsplit(client.authorize_url("st"))
    assert (url.scheme, url.netloc, url.path) == ("https", "auth.test", "/authorize")
    assert parse_qs(url.query) == {
        "client_id": ["vods-admin"], "state": ["st"],
        "redirect_uri": ["https://vods.test/backend-admin/admin/signin/callback"],
    }


async def test_client_talks_to_vexoulz_auth():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/token":
            return httpx.Response(200, json={"user": ALICE, "sid": "s1", "expiresAt": "x"})
        if request.url.path == "/v1/sessions/s1":
            return httpx.Response(200, json={"active": True})
        if request.url.path == "/v1/sessions/gone":
            return httpx.Response(404, json={"active": False})
        return httpx.Response(500)

    client = VexoulzAuth("https://auth.test", "http://auth:8090", "vods-admin", "sec", "https://vods.test/cb",
                         transport=httpx.MockTransport(handler))
    assert await client.redeem("c") == SignedIn(ALICE, "s1")
    assert seen[0].url.host == "auth" and seen[0].headers["authorization"].startswith("Basic ")
    assert await client.active("s1") is True
    assert await client.active("gone") is False
    with pytest.raises(SignInError):
        await client.active("boom")


def test_pending_states_and_safe_next():
    now = [0.0]
    states = PendingStates(ttl_s=10, limit=2, clock=lambda: now[0])
    a = states.start("/admin/a")
    assert states.finish(a) == "/admin/a" and states.finish(a) is None
    b = states.start("/admin/b")
    now[0] = 10
    assert states.finish(b) is None
    first, second, third = states.start("/1"), states.start("/2"), states.start("/3")
    assert states.finish(first) is None and states.finish(third) == "/3" and states.finish(second) == "/2"

    assert safe_next("/admin/vods/1?x=1") == "/admin/vods/1?x=1"
    for bad in (None, "", "https://evil.test", "//evil.test", "/\\evil.test", "admin"):
        assert safe_next(bad) == "/admin"


def test_state_cookie_name_is_distinct():
    assert STATE_COOKIE != SESSION_COOKIE
