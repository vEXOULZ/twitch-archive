import datetime as dt

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from pydantic import SecretStr

from archive_worker import youtube


@pytest.fixture
def yt(settings, monkeypatch):
    settings.google_client_id = "client"
    settings.google_client_secret = SecretStr("secret")
    stored: dict = {}

    async def load():
        return stored.get("token")

    async def save(value):
        stored["token"] = value

    monkeypatch.setattr(youtube, "load_token", load)
    monkeypatch.setattr(youtube, "save_token", save)
    client = youtube.YouTube(settings)
    client.stored = stored
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
    result = await yt.check()
    assert calls == ["r1"]
    assert result == {"authorized": True, "valid": True, "accessTokenExpiry": "2030-01-01T00:00:00+00:00"}
    assert yt.stored["token"]["refresh_token"] == "r2"


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
