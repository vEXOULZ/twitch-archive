"""Browser login for the admin API: password, sessions, CSRF, login rate limit (no database needed)."""

import httpx
import pytest
from pydantic import SecretStr
from starlette.requests import Request

from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import SESSION_COOKIE, AdminAuth, LoginLimiter, client_address, parse_networks


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_password_is_hashed_and_checked():
    auth = AdminAuth("hunter2")
    assert auth.enabled and auth.password is None  # only the scrypt hash is kept
    assert auth.check_password("hunter2")
    assert not auth.check_password("hunter3")
    assert not AdminAuth(None).enabled
    assert not AdminAuth(None).check_password("")


def test_sessions_expire_and_log_out():
    clock = Clock()
    auth = AdminAuth("pw", ttl_s=60, clock=clock)
    session = auth.login()
    assert auth.session(session.token) is session
    assert auth.valid_csrf(session, session.csrf)
    assert not auth.valid_csrf(session, "nope") and not auth.valid_csrf(session, None)
    clock.now += 60
    assert auth.session(session.token) is None
    other = auth.login()
    auth.logout(other.token)
    assert auth.session(other.token) is None


def test_login_limiter_window():
    clock = Clock()
    limiter = LoginLimiter(attempts=5, window_s=300, clock=clock)
    for _ in range(4):
        limiter.failed("a")
        clock.now += 10
    assert limiter.retry_after("a") is None
    limiter.failed("a")
    assert limiter.retry_after("a") == 300 - 40  # until the first failure leaves the window
    assert limiter.retry_after("b") is None  # per address
    clock.now += 261
    assert limiter.retry_after("a") is None
    limiter.reset("a")
    assert limiter.retry_after("a") is None


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request({
        "type": "http",
        "client": (peer, 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


def test_client_address_trusts_only_configured_proxies():
    proxies = parse_networks(["10.0.0.1", "172.16.0.0/12"])
    xff = {"X-Forwarded-For": "6.6.6.6, 1.2.3.4, 172.16.5.5"}
    # Not a proxy: whatever it claims is ignored.
    assert client_address(_request("9.9.9.9", xff), proxies) == "9.9.9.9"
    assert client_address(_request("10.0.0.1", xff), []) == "10.0.0.1"
    # A proxy: the nearest hop that is not one of ours (6.6.6.6 was written by the client).
    assert client_address(_request("10.0.0.1", xff), proxies) == "1.2.3.4"
    assert client_address(_request("10.0.0.1", {"X-Real-IP": "5.5.5.5"}), proxies) == "5.5.5.5"
    assert client_address(_request("10.0.0.1", {}), proxies) == "10.0.0.1"
    with pytest.raises(ValueError):
        parse_networks(["not-an-address"])


@pytest.fixture
def admin(deps):
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.admin_password = SecretStr("correct horse")
    app = create_admin_app(deps, jobs.Runner(deps))

    def client(peer: str = "192.0.2.1") -> httpx.AsyncClient:
        # https: the session cookie is Secure, so the client only sends it back over https.
        transport = httpx.ASGITransport(app=app, client=(peer, 1234))
        return httpx.AsyncClient(transport=transport, base_url="https://admin")

    return client


async def test_login_session_and_logout(admin):
    async with admin() as c:
        assert (await c.get("/admin/session")).json() == {
            "authenticated": False, "csrf": None, "expiresAt": None, "passwordLogin": True,
        }
        assert (await c.get("/admin/kinds")).status_code == 403

        wrong = await c.post("/admin/session", json={"password": "nope"})
        assert wrong.status_code == 401 and wrong.json() == {"error": True, "msg": "Wrong password"}

        r = await c.post("/admin/session", json={"password": "correct horse"})
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] and body["passwordLogin"] and body["csrf"] and body["expiresAt"]
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=28800"):
            assert attr.lower() in cookie.lower()

        assert (await c.get("/admin/session")).json()["csrf"] == body["csrf"]
        assert (await c.get("/admin/kinds")).status_code == 200  # the cookie alone is enough for GET

        # Anything else needs the CSRF token too.
        assert (await c.delete("/admin/session")).status_code == 403
        bad = await c.patch("/admin/jobs/1", json={"pauseNext": True}, headers={"X-CSRF-Token": "x"})
        assert bad.status_code == 403 and "CSRF" in bad.json()["msg"]

        out = await c.delete("/admin/session", headers={"X-CSRF-Token": body["csrf"]})
        assert out.status_code == 204
        assert f"{SESSION_COOKIE}=" in out.headers["set-cookie"] and "max-age=0" in out.headers["set-cookie"].lower()
        assert (await c.get("/admin/session")).json()["authenticated"] is False
        c.cookies.set(SESSION_COOKIE, r.cookies[SESSION_COOKIE])  # the old cookie is dead server-side too
        assert (await c.get("/admin/kinds")).status_code == 403


async def test_api_key_still_works_and_wrong_key_is_refused(admin):
    async with admin() as c:
        assert (await c.get("/admin/kinds", headers={"Authorization": "Bearer k"})).status_code == 200
        assert (await c.get("/admin/kinds", headers={"Authorization": "Bearer x"})).status_code == 403


async def test_failed_logins_are_rate_limited_per_address(admin):
    async with admin("192.0.2.7") as c:
        for _ in range(5):
            assert (await c.post("/admin/session", json={"password": "nope"})).status_code == 401
        limited = await c.post("/admin/session", json={"password": "correct horse"})
        assert limited.status_code == 429 and limited.json()["error"] is True
        assert 0 < int(limited.headers["Retry-After"]) <= 300
    async with admin("192.0.2.8") as c:  # someone else is not locked out
        assert (await c.post("/admin/session", json={"password": "correct horse"})).status_code == 200


async def test_password_login_off_without_a_password(deps):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, jobs.Runner(deps))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.get("/admin/session")).json()["passwordLogin"] is False
        assert (await c.post("/admin/session", json={"password": ""})).status_code == 404
