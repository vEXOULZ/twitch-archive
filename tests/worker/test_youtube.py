import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
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

    monkeypatch.setattr(youtube, "load_token", load)
    monkeypatch.setattr(youtube, "save_token", save)
    client = youtube.YouTube(settings)
    client.stored = stored  # type: ignore[attr-defined]
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
        "channel": CHANNEL,
    }
    assert yt.stored["token"]["refresh_token"] == "r2"


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
