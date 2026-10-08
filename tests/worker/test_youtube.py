import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from archive_common import audit, http
from archive_worker import youtube
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from pydantic import SecretStr


@pytest.fixture
def yt(settings, monkeypatch):
    settings.google_client_id = "client"
    settings.google_client_secret = SecretStr("secret")
    stored: dict[str, Any] = {}

    async def load():
        return stored.get("token")

    async def save(value):
        stored["token"] = value

    async def write(entry):
        audited.append(entry)
        return len(audited)

    audited: list[Any] = []
    monkeypatch.setattr(youtube, "load_token", load)
    monkeypatch.setattr(youtube, "save_token", save)
    monkeypatch.setattr(audit, "write", write)
    client = youtube.YouTube(settings)
    client.stored = stored  # type: ignore[attr-defined]
    client.audited = audited  # type: ignore[attr-defined]
    return client


async def test_check_without_token(yt):
    result = await yt.check()
    assert result["authorized"] is False and result["valid"] is False
    assert "No YouTube token stored" in result["error"]


async def test_check_reports_revoked_token(yt, monkeypatch):
    yt.stored["token"] = {"refresh_token": "dead", "token": "stale", "scopes": None}

    def refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refresh)
    result = await yt.check()
    assert result == {
        "authorized": True,
        "valid": False,
        "error": "RefreshError: invalid_grant: Token has been expired or revoked.",
    }
    [entry] = yt.audited
    assert (entry.action, entry.outcome, entry.target) == ("youtube.token.refresh", "failed", "youtube")
    assert entry.detail == {"reason": "check", "error": result["error"]}


async def test_check_always_refreshes_and_keeps_rotated_token(yt, monkeypatch):
    # "stale" has no expiry, so google-auth would treat it as valid; check() must refresh anyway.
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None}
    calls = []

    def refresh(self, request):
        calls.append(self.refresh_token)
        self.token = "fresh"
        self._refresh_token = "r2"
        self.expiry = dt.datetime(2030, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", refresh)
    monkeypatch.setattr(yt, "own_channel", _returns(CHANNEL))
    result = await yt.check()
    assert calls == ["r1"]
    assert result == {
        "authorized": True,
        "valid": True,
        "accessTokenExpiry": "2030-01-01T00:00:00+00:00",
        "connectedAt": None,
        "refreshTokenExpiresAt": None,
        "channel": CHANNEL,
    }
    saved = yt.stored["token"]
    assert saved["refresh_token"] == "r2" and saved["token"] == "fresh"
    assert saved["refreshTokenRotatedAt"] == saved["refreshedAt"]
    [entry] = yt.audited
    expiry = result["accessTokenExpiry"]
    assert entry.detail == {"reason": "check", "changed": ["refreshToken"], "accessTokenExpiry": expiry}
    assert entry.before == {"refreshToken": youtube.fingerprint("r1")}
    assert entry.after == {"refreshToken": youtube.fingerprint("r2")}
    assert "'r1'" not in repr(entry) and "'r2'" not in repr(entry)  # never the token itself


CHANNEL = {"id": "UC123", "title": "keeki", "url": "https://www.youtube.com/@keeki"}


def _returns(value):
    async def fn():
        if isinstance(value, Exception):
            raise value
        return value

    return fn


def _fresh(monkeypatch):
    def refresh(self, request):
        self.token = "fresh"

    monkeypatch.setattr(Credentials, "refresh", refresh)


async def test_every_refresh_is_saved_and_audited(yt, monkeypatch, caplog):
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None, "connectedAt": "then"}
    _fresh(monkeypatch)
    monkeypatch.setattr(yt, "own_channel", _returns(CHANNEL))
    with caplog.at_level(logging.WARNING):
        await yt.check()
    saved = yt.stored["token"]
    assert saved["token"] == "fresh" and saved["refresh_token"] == "r1" and saved["connectedAt"] == "then"
    assert "refreshedAt" in saved and "refreshTokenRotatedAt" not in saved
    [entry] = yt.audited
    assert entry.outcome == "ok" and entry.before is None and entry.after is None
    assert entry.detail == {"reason": "check", "changed": [], "accessTokenExpiry": None}
    assert "new YouTube refresh token" not in caplog.text


async def test_a_refresh_google_auth_makes_itself_is_saved_too(yt, monkeypatch):
    # An access token that expires mid-upload: googleapiclient refreshes it in the upload's thread.
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None}
    _fresh(monkeypatch)
    creds = await yt._credentials()
    await asyncio.to_thread(creds.refresh, None)
    await yt._settle()
    assert yt.stored["token"]["token"] == "fresh"
    assert [e.detail["reason"] for e in yt.audited] == ["expired"]


async def test_a_change_of_scopes_is_audited(yt, monkeypatch):
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None, "grantedScopes": ["a"]}

    def refresh(self, request):
        self.token = "fresh"
        self._granted_scopes = ["b", "a"]

    monkeypatch.setattr(Credentials, "refresh", refresh)
    monkeypatch.setattr(yt, "own_channel", _returns(CHANNEL))
    await yt.check()
    [entry] = yt.audited
    assert entry.detail["changed"] == ["grantedScopes"]
    assert (entry.before, entry.after) == ({"grantedScopes": ["a"]}, {"grantedScopes": ["a", "b"]})
    assert yt.stored["token"]["grantedScopes"] == ["a", "b"]


def test_token_summary_has_no_secrets():
    token = {"refresh_token": "r1", "token": "a1", "connectedAt": "then", "grantedScopes": ["s"]}
    assert youtube.token_summary(token) == {
        "refreshToken": youtube.fingerprint("r1"),
        "connectedAt": "then",
        "refreshTokenExpiresAt": None,
        "refreshTokenRotatedAt": None,
        "grantedScopes": ["s"],
    }
    assert youtube.token_summary(None) is None


async def test_check_reports_when_the_token_was_connected(yt, monkeypatch):
    when = {"connectedAt": "2026-10-01T12:00:00+00:00", "refreshTokenExpiresAt": "2026-10-08T12:00:00+00:00"}
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None, **when}
    _fresh(monkeypatch)
    monkeypatch.setattr(yt, "own_channel", _returns(CHANNEL))
    result = await yt.check()
    assert result["connectedAt"] == when["connectedAt"]
    assert result["refreshTokenExpiresAt"] == when["refreshTokenExpiresAt"]


@pytest.mark.parametrize(("reply", "lifetime"), [({"refresh_token_expires_in": 604799}, 604799), ({}, None)])
async def test_exchange_code_dates_the_token(yt, settings, monkeypatch, reply, lifetime):
    async def request(method, url, **kwargs):
        data = {"access_token": "a", "refresh_token": "r", "expires_in": 3599, "scope": "s2 s1", **reply}
        return SimpleNamespace(json=lambda: data)

    monkeypatch.setattr(http, "request", request)
    before = dt.datetime.now(dt.UTC)
    token = await youtube.exchange_code(settings, "code")
    connected = dt.datetime.fromisoformat(token["connectedAt"])
    assert before <= connected <= dt.datetime.now(dt.UTC)
    if lifetime is None:
        assert token["refreshTokenExpiresAt"] is None
    else:
        assert dt.datetime.fromisoformat(token["refreshTokenExpiresAt"]) - connected == dt.timedelta(seconds=lifetime)
    assert token["grantedScopes"] == ["s1", "s2"]
    assert yt.stored["token"] == token


async def test_check_flags_an_account_without_a_channel(yt, monkeypatch):
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None}
    _fresh(monkeypatch)
    monkeypatch.setattr(yt, "own_channel", _returns(None))
    result = await yt.check()
    assert result["authorized"] is True and result["valid"] is False and result["channel"] is None
    assert result["error"] == youtube.NO_CHANNEL


async def test_check_stays_valid_when_the_channel_lookup_fails(yt, monkeypatch):
    yt.stored["token"] = {"refresh_token": "r1", "token": "stale", "scopes": None}
    _fresh(monkeypatch)
    monkeypatch.setattr(yt, "own_channel", _returns(OSError("down")))
    result = await yt.check()
    assert result["valid"] is True and "channel" not in result


class _Channels:
    def __init__(self, outcome):
        self.outcome = outcome

    def channels(self):
        return self

    def list(self, **params):
        assert params == {"part": "snippet", "mine": True}
        return self

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"items": [{"id": "UC123", "snippet": {"title": "keeki", "customUrl": "@keeki"}}]},
            CHANNEL,
        ),
        (
            {"items": [{"id": "UC123", "snippet": {"title": "keeki", "customUrl": "oldname"}}]},
            {"id": "UC123", "title": "keeki", "url": "https://www.youtube.com/channel/UC123"},
        ),
        ({"items": []}, None),
        ({}, None),
    ],
)
async def test_own_channel(yt, monkeypatch, response, expected):
    async def service():
        return _Channels(response)

    monkeypatch.setattr(yt, "_service", service)
    assert await yt.own_channel() == expected


async def test_own_channel_signup_required_is_no_channel(yt, monkeypatch):
    content = b'{"error": {"errors": [{"reason": "youtubeSignupRequired"}], "message": "Unauthorized"}}'
    signup = HttpError(SimpleNamespace(status=401, reason="Unauthorized"), content)
    other = HttpError(
        SimpleNamespace(status=403, reason="Forbidden"), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}'
    )

    async def service(outcome):
        return _Channels(outcome)

    monkeypatch.setattr(yt, "_service", lambda: service(signup))
    assert await yt.own_channel() is None
    monkeypatch.setattr(yt, "_service", lambda: service(other))
    with pytest.raises(HttpError):
        await yt.own_channel()


async def test_check_unconfigured(settings):
    result = await youtube.YouTube(settings).check()
    assert result["valid"] is False and "GOOGLE_CLIENT_ID" in result["error"]


async def test_cached_check_refreshes_at_most_every_max_age(yt, monkeypatch):
    calls = []

    async def fake_check():
        calls.append(1)
        return {"authorized": True, "valid": True, "accessTokenExpiry": None}

    monkeypatch.setattr(yt, "_check", fake_check)
    first = await yt.cached_check(600)
    assert first["valid"] and first["checkedAt"].tzinfo is not None
    assert (await yt.cached_check(600)) is first and len(calls) == 1
    await yt.check()  # a forced check (GET /admin/youtube/status) refreshes the cache too
    assert len(calls) == 2 and yt.last_check is not first
    yt.last_check["checkedAt"] -= dt.timedelta(seconds=601)
    await yt.cached_check(600)
    assert len(calls) == 3
