"""The linked Twitch account for live playback tokens without stitched ads: GQL, linking, refresh."""

import datetime as dt
import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.twitch.gql import GQL_URL, Gql, ViewerAuth
from archive_worker import twitch_viewer
from archive_worker.admin import create_admin_app
from pydantic import SecretStr
from sqlalchemy import delete, func, select

# Not real tokens: stand-ins the mocked Twitch hands out and accepts.
DEVICE_TOKEN = "device-access-stand-in"
PASTED_TOKEN = "pasted-cookie-stand-in"
REFRESHED_TOKEN = "refreshed-access-stand-in"
CLIENT = "test-client-id"
VIEWER = ViewerAuth(client_id=CLIENT, token=DEVICE_TOKEN, login="vexoulz")


def _token(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    key = "streamPlaybackAccessToken" if body["variables"]["isLive"] else "videoPlaybackAccessToken"
    return httpx.Response(200, json={"data": {key: {"value": '{"t":1}', "signature": "abc"}}})


def _gql(settings, auth: ViewerAuth | None) -> Gql:
    gql = Gql(settings)

    async def viewer() -> ViewerAuth | None:
        return auth

    gql.viewer = viewer
    return gql


@respx.mock
async def test_live_token_is_asked_for_as_the_linked_account(settings):
    route = respx.post(GQL_URL).mock(side_effect=_token)
    tok = await _gql(settings, VIEWER).live_access_token("vexoulz")
    assert tok.viewer == "vexoulz"
    sent = route.calls.last.request.headers
    assert sent["Authorization"] == f"OAuth {DEVICE_TOKEN}" and sent["Client-Id"] == CLIENT


@respx.mock
async def test_vod_tokens_stay_anonymous(settings):
    route = respx.post(GQL_URL).mock(side_effect=_token)
    await _gql(settings, VIEWER).vod_access_token("100")
    sent = route.calls.last.request.headers
    assert "Authorization" not in sent and sent["Client-Id"] == settings.gql_client_id


@respx.mock
async def test_a_refused_viewer_token_falls_back_to_anonymous(settings):
    def refuse_signed_in(request: httpx.Request) -> httpx.Response:
        if "Authorization" in request.headers:
            return httpx.Response(401, json={"message": "invalid token"})
        return _token(request)

    route = respx.post(GQL_URL).mock(side_effect=refuse_signed_in)
    tok = await _gql(settings, VIEWER).live_access_token("vexoulz")
    assert tok.viewer is None and route.call_count == 2
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_a_failing_viewer_lookup_never_stops_a_recording(settings):
    respx.post(GQL_URL).mock(side_effect=_token)
    gql = Gql(settings)

    async def broken() -> ViewerAuth | None:
        raise OSError("database down")

    gql.viewer = broken
    assert (await gql.live_access_token("vexoulz")).viewer is None


# ── With the database ─────────────────────────────────────────────────────


@pytest.fixture
async def clean(db):
    async with get_sessionmaker()() as s:
        before = (await s.execute(select(func.max(AUDIT_LOG.c.id)))).scalar() or 0
    saved = await twitch_viewer.load_token()
    await twitch_viewer.clear_token()
    yield
    await twitch_viewer.clear_token()
    if saved is not None:
        await twitch_viewer.save_token(saved)
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.id > before, AUDIT_LOG.c.target == "twitch-viewer"))
        await s.commit()


async def _audit() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.target == "twitch-viewer").order_by(AUDIT_LOG.c.id))
        ).all()
    return [dict(r._mapping) for r in rows]


def _validate(request: httpx.Request) -> httpx.Response:
    token = request.headers["Authorization"].removeprefix("OAuth ")
    if token not in (DEVICE_TOKEN, PASTED_TOKEN, REFRESHED_TOKEN):
        return httpx.Response(401, json={"status": 401, "message": "invalid access token"})
    client = "web-client" if token == PASTED_TOKEN else CLIENT
    return httpx.Response(
        200, json={"client_id": client, "login": "vexoulz", "user_id": "38656648", "scopes": [], "expires_in": 0}
    )


async def test_link_with_the_device_flow_then_unlink(clean, deps, make_service, respx_mock):
    deps.settings.admin_api_key = SecretStr("k")
    deps.settings.twitch_viewer_client_id = CLIENT
    app = create_admin_app(deps, await make_service(start=False))
    key = {"Authorization": "Bearer k"}
    respx_mock.post(twitch_viewer.DEVICE_URL).respond(
        json={
            "device_code": "device-code-stand-in",
            "user_code": "ABCDEFGH",
            "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH",
            "expires_in": 1800,
            "interval": 5,
        }
    )
    entered = False

    def token(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["device_code"] == ["device-code-stand-in"] and form["client_id"] == [CLIENT]
        if not entered:
            return httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
        return httpx.Response(200, json={"access_token": DEVICE_TOKEN, "refresh_token": "r", "expires_in": 14000})

    respx_mock.post(twitch_viewer.TOKEN_URL).mock(side_effect=token)
    respx_mock.get(twitch_viewer.VALIDATE_URL).mock(side_effect=_validate)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        assert (await c.get("/admin/twitch/viewer", headers=key)).json()["linked"] is False
        flow = (await c.post("/admin/twitch/viewer/device", headers=key)).json()
        assert flow["userCode"] == "ABCDEFGH" and "device_code" not in json.dumps(flow)
        poll = f"/admin/twitch/viewer/device/{flow['flowId']}"
        assert (await c.get(poll, headers=key)).json() == {"state": "pending"}
        entered = True
        done = (await c.get(poll, headers=key)).json()
        assert done["state"] == "linked"
        assert done["status"]["valid"] is True and done["status"]["login"] == "vexoulz"
        assert done["status"]["method"] == "device" and "token" not in done["status"]
        assert (await c.get(poll, headers=key)).json()["state"] == "failed"  # a flow links once

        auth = await deps.viewer.auth()
        assert auth == ViewerAuth(client_id=CLIENT, token=DEVICE_TOKEN, login="vexoulz")
        health = (await c.get("/admin/health", headers=key)).json()
        assert health["twitchViewer"]["linked"] is True and health["twitchViewer"]["checkedAt"]

        assert (await c.delete("/admin/twitch/viewer", headers=key)).status_code == 200
        assert await twitch_viewer.load_token() is None and await deps.viewer.auth() is None

    rows = await _audit()
    assert [r["action"] for r in rows] == ["twitch_viewer.link.start", "twitch_viewer.link", "twitch_viewer.unlink"]
    assert rows[1]["after"]["login"] == "vexoulz" and rows[2]["before"]["login"] == "vexoulz"
    assert DEVICE_TOKEN not in json.dumps([r["before"] for r in rows] + [r["after"] for r in rows], default=str)


async def test_a_refused_device_flow_says_why(clean, deps, make_service, respx_mock):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    respx_mock.post(twitch_viewer.DEVICE_URL).respond(400, json={"status": 400, "message": "invalid client"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        resp = await c.post("/admin/twitch/viewer/device", headers={"Authorization": "Bearer k"})
    # A 4xx, not a 502 Cloudflare would swap for its own page.
    assert resp.status_code == 400 and "invalid client" in resp.text


async def test_link_by_pasting_a_token(clean, deps, make_service, respx_mock):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    key = {"Authorization": "Bearer k"}
    respx_mock.get(twitch_viewer.VALIDATE_URL).mock(side_effect=_validate)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as c:
        bad = await c.put("/admin/twitch/viewer", headers=key, json={"token": "not-a-token"})
        assert bad.status_code == 400 and "invalid access token" in bad.text
        assert (await c.put("/admin/twitch/viewer", headers=key, json={})).status_code == 400
        ok = (await c.put("/admin/twitch/viewer", headers=key, json={"token": f"OAuth {PASTED_TOKEN}"})).json()
        assert ok["valid"] is True and ok["method"] == "paste" and ok["clientId"] == "web-client"

    # The token goes with the client id it was issued to, which GQL wants.
    assert await deps.viewer.auth() == ViewerAuth(client_id="web-client", token=PASTED_TOKEN, login="vexoulz")
    rows = await _audit()
    assert [r["action"] for r in rows] == ["twitch_viewer.paste"]
    assert PASTED_TOKEN not in json.dumps(rows, default=str)


async def test_an_expiring_token_is_refreshed(clean, settings, respx_mock):
    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)).isoformat()
    await twitch_viewer.save_token(
        {"token": DEVICE_TOKEN, "refreshToken": "r1", "clientId": CLIENT, "login": "vexoulz", "expiresAt": soon}
    )
    refresh = respx_mock.post(twitch_viewer.TOKEN_URL).respond(
        json={"access_token": REFRESHED_TOKEN, "refresh_token": "r2", "expires_in": 14000}
    )
    viewer = twitch_viewer.TwitchViewer(settings)
    assert (await viewer.auth()) == ViewerAuth(client_id=CLIENT, token=REFRESHED_TOKEN, login="vexoulz")
    assert parse_qs(refresh.calls.last.request.content.decode())["refresh_token"] == ["r1"]
    stored = await twitch_viewer.load_token()
    assert stored is not None and stored["refreshToken"] == "r2" and stored["expiresAt"] > soon
    await viewer.auth()
    assert refresh.call_count == 1  # good for hours now


async def test_an_expired_token_that_cannot_refresh_is_not_sent(clean, settings, respx_mock):
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=60)).isoformat()
    await twitch_viewer.save_token(
        {"token": DEVICE_TOKEN, "refreshToken": "r1", "clientId": CLIENT, "login": "vexoulz", "expiresAt": past}
    )
    respx_mock.post(twitch_viewer.TOKEN_URL).respond(400, json={"status": 400, "message": "Invalid refresh token"})
    viewer = twitch_viewer.TwitchViewer(settings)
    assert await viewer.auth() is None
    status = await viewer.check()
    assert status["linked"] is True and status["valid"] is False and "link the account again" in status["error"]
