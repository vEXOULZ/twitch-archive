"""The admin password is local-only; Twitch sign-in through vexoulz-auth (faked here) works from anywhere."""

import json
from typing import Any
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
    with_admin,
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
        self.refuse: str | None = None
        self.checks = 0
        self.fetched: list[bool] = []  # per redemption: was it a POST /v1/codes code

    def authorize_url(self, state: str) -> str:
        return f"https://auth.test/authorize?client_id=vods-admin&state={state}"

    def approve(self, user: dict[str, Any], sid: str = "sid-1") -> str:
        code = f"code-{len(self.codes)}"
        self.codes[code] = SignedIn(user, sid)
        return code

    async def redeem(self, code: str, fetched: bool = False) -> SignedIn:
        self.fetched.append(fetched)
        if self.down:
            raise SignInError("down")
        if self.refuse:
            raise SignInError("refused", self.refuse)
        try:
            return self.codes.pop(code)
        except KeyError:
            raise SignInError("refused", "expired") from None  # invalid_grant

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
    deps.settings.admin_site_origins = ["https://vods.test/"]

    def client(peer: str = "203.0.113.5", *, networks: list[str] | None = None, signin=fake) -> httpx.AsyncClient:
        if networks is not None:
            deps.settings.admin_password_networks = networks
        app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)), signin=signin)
        transport = httpx.ASGITransport(app=app, client=(peer, 1234))
        return httpx.AsyncClient(transport=transport, base_url="https://admin")

    return client


async def sign_in(
    c: httpx.AsyncClient, fake: FakeAuth, user: dict[str, Any] = ALICE, next: str = "/admin/jobs", sid: str = "sid-1"
) -> httpx.Response:
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
    async with admin("192.168.1.20") as c:  # conventions:allow-infra
        assert (await c.get("/admin/session")).json()["passwordLogin"] is True
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200
    async with admin("::1") as c:
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200


async def test_password_networks_can_be_opened_or_narrowed(admin):
    async with admin("203.0.113.5", networks=["*"]) as c:
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200
    async with admin("192.168.1.20", networks=["10.0.0.0/8"]) as c:  # conventions:allow-infra
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 403


async def test_password_behind_a_proxy_counts_the_real_client(admin, deps):
    deps.settings.admin_trusted_proxies = ["127.0.0.1"]
    async with admin("127.0.0.1") as c:
        outside = {"X-Forwarded-For": "203.0.113.9"}
        assert (await c.post("/admin/session", json={"password": "correct horse"}, headers=outside)).status_code == 403
        inside = {"X-Forwarded-For": "192.168.1.9"}  # conventions:allow-infra
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
        assert (
            await c.get("/admin/signin/callback", params={"code": fake.approve(ALICE), "state": state})
        ).status_code == 302
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

        fake.refuse = "misconfigured"
        assert error_of(await sign_in(c, fake)) == "misconfigured"
        fake.refuse = None

        fake.down = True
        assert error_of(await sign_in(c, fake)) == "unavailable"


async def quiet(c: httpx.AsyncClient, next: str = "/vods/1?t=5", **callback) -> httpx.Response:
    start = await c.get("/admin/signin", params={"next": next, "quiet": "1"})
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await c.get("/admin/signin/callback", params={"state": state, **callback})


async def test_quiet_sign_in_answers_admin_1_or_0_and_never_the_login_page(admin, fake):
    async with admin() as c:
        ok = await quiet(c, code=fake.approve(ALICE))
        assert ok.status_code == 302 and ok.headers["location"] == "/vods/1?t=5&admin=1"
        session = (await c.get("/admin/session")).json()
        assert session["authenticated"] and session["user"]["login"] == "alice"
        assert (await c.patch("/admin/jobs/1", json={"pauseNext": True})).status_code == 403  # CSRF still applies

    for callback in ({"code": fake.approve(MALLORY)}, {"error": "denied"}, {"error": "<odd>"}):
        async with admin() as c:
            r = await quiet(c, **callback)
            assert r.headers["location"] == "/vods/1?t=5&admin=0"
            assert STATE_COOKIE in r.headers["set-cookie"]  # the state cookie is cleared
            assert (await c.get("/admin/session")).json()["authenticated"] is False

    for trouble in ("down", "refuse"):
        async with admin() as c:
            fake.down, fake.refuse = trouble == "down", "misconfigured" if trouble == "refuse" else None
            r = await quiet(c, code=fake.approve(ALICE))
            assert r.headers["location"] == "/vods/1?t=5&admin=0"
    fake.down, fake.refuse = False, None


async def test_sign_out_everywhere_is_noticed_after_check_interval(deps, fake):
    from archive_worker.admin_signin import CHECK_S

    deps.settings.admin_twitch_ids = ["100"]
    app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)), signin=fake)
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


# ── Code sign-in: POST /admin/session {code} ─────────────────────────────

SITE = {"Origin": "https://vods.test"}


def problem_code(r: httpx.Response, status: int) -> str:
    assert r.status_code == status and r.headers["content-type"] == "application/problem+json"
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    return r.json()["code"]  # type: ignore[no-any-return]


async def test_code_sign_in_makes_the_dashboard_session_for_an_admin(admin, fake):
    async with admin() as c:
        r = await c.post("/admin/session", json={"code": fake.approve(ALICE)}, headers=SITE)
        assert r.status_code == 200 and fake.fetched == [True]  # redeemed with the code's redirect URI
        body = r.json()
        assert body["admin"] is True and body["authenticated"] is True and body["user"] == ALICE and body["csrf"]
        cookie = r.headers["set-cookie"].lower()
        for attr in (f"{SESSION_COOKIE}=", "httponly", "secure", "samesite=strict", "path=/", "max-age=28800"):
            assert attr in cookie
        assert r.headers["access-control-allow-origin"] == "https://vods.test"
        assert r.headers["access-control-allow-credentials"] == "true" and "Origin" in r.headers["vary"]

        session = (await c.get("/admin/session", headers=SITE)).json()
        assert session["authenticated"] and session["user"] == ALICE and session["csrf"] == body["csrf"]
        assert (await c.get("/admin/kinds")).status_code == 200
        assert (await c.patch("/admin/jobs/1", json={"pauseNext": True})).status_code == 403  # CSRF still applies


async def test_code_sign_in_without_an_origin_header_works(admin, fake):
    async with admin() as c:  # not a browser: nothing for CORS to say
        r = await c.post("/admin/session", json={"code": fake.approve(ALICE)})
        assert r.status_code == 200 and r.json()["admin"] is True
        assert "access-control-allow-origin" not in r.headers


async def test_code_sign_in_answers_not_an_admin_without_a_cookie(admin, fake):
    async with admin() as c:
        r = await c.post("/admin/session", json={"code": fake.approve(MALLORY)}, headers=SITE)
        assert r.status_code == 200 and r.json() == {"admin": False}
        assert "set-cookie" not in r.headers
        assert r.headers["access-control-allow-origin"] == "https://vods.test"
        assert (await c.get("/admin/session")).json()["authenticated"] is False
        assert (await c.get("/admin/kinds")).status_code == 403


async def test_code_sign_in_refuses_a_bad_code_with_problem_details(admin, fake):
    async with admin() as c:
        made_up = await c.post("/admin/session", json={"code": "made-up"}, headers=SITE)
        assert problem_code(made_up, 400) == "invalid_code"
        code = fake.approve(ALICE)
        assert (await c.post("/admin/session", json={"code": code}, headers=SITE)).status_code == 200
        c.cookies.clear()
        reused = await c.post("/admin/session", json={"code": code}, headers=SITE)
        assert problem_code(reused, 400) == "invalid_code"
        assert reused.headers["access-control-allow-origin"] == "https://vods.test"  # the site can read why
        for missing in ({"code": ""}, {"code": None}, {"code": 5}):
            assert problem_code(await c.post("/admin/session", json=missing, headers=SITE), 400) == "invalid_code"
        assert (await c.get("/admin/session")).json()["authenticated"] is False

        fake.refuse = "expired"  # what VexoulzAuth makes of invalid_grant (expired, used, signed out meanwhile)
        r = await c.post("/admin/session", json={"code": fake.approve(ALICE)}, headers=SITE)
        assert problem_code(r, 400) == "invalid_code"
        fake.refuse = "misconfigured"
        assert problem_code(await c.post("/admin/session", json={"code": "x"}, headers=SITE), 502) == "misconfigured"
        fake.refuse, fake.down = None, True
        r = await c.post("/admin/session", json={"code": fake.approve(ALICE)}, headers=SITE)
        assert problem_code(r, 503) == "unavailable"
    fake.down = False


async def test_code_sign_in_refuses_other_origins(admin, fake):
    evil = {"Origin": "https://evil.test"}
    async with admin() as c:
        code = fake.approve(ALICE)
        r = await c.post("/admin/session", json={"code": code}, headers=evil)
        assert problem_code(r, 403) == "origin_not_allowed"
        assert "access-control-allow-origin" not in r.headers
        assert code in fake.codes and fake.fetched == []  # never sent to vexoulz-auth

        preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
        refused = await c.options("/admin/session", headers={**evil, **preflight})
        assert refused.status_code == 403 and "access-control-allow-origin" not in refused.headers
        ok = await c.options("/admin/session", headers={**SITE, **preflight})
        assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == "https://vods.test"
        assert ok.headers["access-control-allow-credentials"] == "true"
        assert "POST" in ok.headers["access-control-allow-methods"]
        assert "content-type" in ok.headers["access-control-allow-headers"].lower()

        # Only /admin/session answers CORS; the rest of the API stays same-origin.
        assert "access-control-allow-origin" not in (await c.get("/admin/kinds", headers=SITE)).headers

        assert (await c.post("/admin/session", json={"code": code}, headers=SITE)).status_code == 200


async def test_code_session_is_rechecked_with_vexoulz_auth(deps, fake):
    from archive_worker.admin_signin import CHECK_S

    deps.settings.admin_twitch_ids = ["100"]
    app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)), signin=fake)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 1234))
    clock = Clock()
    app.state.admin_sessions.clock = clock
    async with httpx.AsyncClient(transport=transport, base_url="https://admin") as c:
        r = await c.post("/admin/session", json={"code": fake.approve(ALICE, sid="sid-7")})
        assert r.json()["admin"] is True
        assert (await c.get("/admin/kinds")).status_code == 200 and fake.checks == 0

        clock.now += CHECK_S
        assert (await c.get("/admin/kinds")).status_code == 200 and fake.checks == 1  # still signed in

        fake.signed_out.add("sid-7")  # "sign out everywhere" on any site
        clock.now += CHECK_S
        assert (await c.get("/admin/kinds")).status_code == 403
        assert (await c.get("/admin/session")).json()["authenticated"] is False


async def test_code_sign_in_off_without_settings(deps):
    app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert problem_code(await c.post("/admin/session", json={"code": "c"}), 404) == "signin_off"


async def test_twitch_sign_in_off_without_settings(deps):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, jobs.JobService(deps, jobs.create_runtime(deps)))
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
        "client_id": ["vods-admin"],
        "state": ["st"],
        "redirect_uri": ["https://vods.test/backend-admin/admin/signin/callback"],
    }
    assert client.code_redirect_uri == client.redirect_uri
    settings.admin_auth_code_redirect_url = "https://vods.test/first"
    built = VexoulzAuth.from_settings(settings)
    assert built is not None and built.code_redirect_uri == "https://vods.test/first"


async def test_a_fetched_code_is_redeemed_with_the_first_registered_redirect_uri():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"user": ALICE, "sid": "s1", "expiresAt": "x"})

    client = VexoulzAuth(
        "https://auth.test",
        "",
        "vods-admin",
        "sec",
        "https://vods.test/cb",
        transport=httpx.MockTransport(handler),
        code_redirect_uri="https://vods.test/first",
    )
    await client.redeem("a")
    await client.redeem("b", fetched=True)
    assert [b["redirect_uri"] for b in bodies] == ["https://vods.test/cb", "https://vods.test/first"]


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

    client = VexoulzAuth(
        "https://auth.test",
        "http://auth:8090",
        "vods-admin",
        "sec",
        "https://vods.test/cb",
        transport=httpx.MockTransport(handler),
    )
    assert await client.redeem("c") == SignedIn(ALICE, "s1")
    assert seen[0].url.host == "auth" and seen[0].headers["authorization"].startswith("Basic ")
    assert json.loads(seen[0].content) == {"code": "c", "redirect_uri": "https://vods.test/cb"}
    assert await client.active("s1") is True
    assert await client.active("gone") is False
    with pytest.raises(SignInError):
        await client.active("boom")


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"error": "invalid_client"}, "misconfigured"),
        (400, {"error": "invalid_request"}, "misconfigured"),
        (400, {"error": "invalid_grant"}, "expired"),
        (429, {"error": "rate_limited"}, "unavailable"),
        (502, None, "unavailable"),
    ],
)
async def test_refusals_say_why(status, body, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body) if body else httpx.Response(status, text="bad gateway")

    client = VexoulzAuth(
        "https://auth.test", "", "vods-admin", "sec", "https://vods.test/cb", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SignInError) as caught:
        await client.redeem("c")
    assert caught.value.reason == reason


async def test_unreachable_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = VexoulzAuth(
        "https://auth.test", "", "vods-admin", "sec", "https://vods.test/cb", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SignInError) as caught:
        await client.redeem("c")
    assert caught.value.reason == "unavailable"


def test_pending_states_and_safe_next():
    now = [0.0]
    states = PendingStates(ttl_s=10, limit=2, clock=lambda: now[0])
    a = states.start("/admin/a")
    done = states.finish(a)
    assert done is not None and done.next == "/admin/a" and not done.quiet and states.finish(a) is None
    b = states.start("/admin/b")
    now[0] = 10
    assert states.finish(b) is None
    first, second, third = states.start("/1"), states.start("/2", quiet=True), states.start("/3")
    assert states.finish(first) is None and states.finish(third).next == "/3"  # type: ignore[union-attr]
    assert states.finish(second)[:2] == ("/2", True)  # type: ignore[index]

    assert with_admin("/vods/1", True) == "/vods/1?admin=1"
    assert with_admin("/vods/1?t=90&admin=1#chat", False) == "/vods/1?t=90&admin=0#chat"

    assert safe_next("/admin/vods/1?x=1") == "/admin/vods/1?x=1"
    for bad in (None, "", "https://evil.test", "//evil.test", "/\\evil.test", "admin"):
        assert safe_next(bad) == "/admin"


def test_state_cookie_name_is_distinct():
    assert STATE_COOKIE != SESSION_COOKIE
