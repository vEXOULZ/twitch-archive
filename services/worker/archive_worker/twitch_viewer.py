"""The broadcaster's own Twitch account, for live playback tokens without stitched ads.

A live recording misses the ads Twitch stitches into the stream (the recording's ``gaps``). A playback
token asked for as an account that sees no ads on the channel (Turbo, or a sub where the channel gives
subs ad-free viewing) gets a stream without them. The account is linked from the Manage screen with
Twitch's device flow (a code entered at twitch.tv/activate) or by pasting a token, and the token lives
in the ``app_state`` table (key ``twitch_viewer_oauth``), like the YouTube one. It is only ever sent
with the live ``PlaybackAccessToken`` request; anything that goes wrong with it falls back to asking
anonymously, so a bad token never stops a recording.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from archive_common import audit, http
from archive_common.config import Settings
from archive_common.db import execute, get_sessionmaker
from archive_common.models import AppState
from archive_common.twitch.gql import ViewerAuth
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from vex_platform.actor import Actor
from vex_platform.audit import AuditEntry

log = logging.getLogger(__name__)

STATE_KEY = "twitch_viewer_oauth"
AUDIT_TARGET = "twitch-viewer"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Refresh an expiring access token this long before it ends.
REFRESH_MARGIN = 600
# How long auth() trusts what it last read from the database.
CACHE_SECONDS = 300


class ViewerTokenError(RuntimeError):
    pass


async def load_token() -> dict[str, Any] | None:
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
        return row.value if row else None


async def save_token(value: dict[str, Any]) -> None:
    stmt = insert(AppState).values(key=STATE_KEY, value=value)
    await execute(stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": value}))


async def clear_token() -> None:
    await execute(delete(AppState).where(AppState.key == STATE_KEY))


def fingerprint(token: str | None) -> str | None:
    """A token's stand-in for logs and the audit log, which never get the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else None


def token_summary(token: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored token without its secrets: for the audit log and the dashboard."""
    if not token:
        return None
    keys = ("login", "userId", "clientId", "method", "connectedAt", "expiresAt")
    return {"token": fingerprint(token.get("token")), **{k: token.get(k) for k in keys}}


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    return str(data.get("message") or data.get("error") or data) if isinstance(data, dict) else str(data)


def _expires_at(seconds: Any) -> str | None:
    """Twitch's ``expires_in`` as a time; 0 or none means the token doesn't expire."""
    if not seconds:
        return None
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(seconds))).isoformat()


async def validate(access_token: str) -> dict[str, Any]:
    """Twitch's view of a token: ``client_id``, ``login``, ``user_id``, ``expires_in``."""
    try:
        resp = await http.request("GET", VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}, attempts=2)
    except httpx.HTTPStatusError as exc:
        raise ViewerTokenError(f"Twitch refused the token: {_message(exc.response)}") from exc
    data = resp.json()
    if not isinstance(data, dict) or not data.get("client_id") or not data.get("login"):
        raise ViewerTokenError("Twitch's validation gave no account for the token (an app token?)")
    return data


async def store(
    access_token: str, *, refresh_token: str | None = None, method: str, expires_in: Any = None
) -> dict[str, Any]:
    """Validate a token and keep it, with the client id it belongs to (GQL wants the two to match)."""
    info = await validate(access_token)
    token = {
        "token": access_token,
        "refreshToken": refresh_token,
        "clientId": info["client_id"],
        "login": info["login"],
        "userId": info.get("user_id"),
        "method": method,
        "connectedAt": dt.datetime.now(dt.UTC).isoformat(),
        "expiresAt": _expires_at(info.get("expires_in") or expires_in),
    }
    await save_token(token)
    return token


# ── Device flow (the Manage screen's button) ──────────────────────────────


async def start_device(settings: Settings) -> dict[str, Any]:
    """Begin a device flow: the code the broadcaster enters at twitch.tv/activate, and the
    ``device_code`` to poll with (kept by the worker, never sent to the browser)."""
    if not settings.twitch_viewer_client_id:
        raise ViewerTokenError("twitch_viewer_client_id is not configured")
    try:
        resp = await http.request(
            "POST", DEVICE_URL, data={"client_id": settings.twitch_viewer_client_id, "scopes": ""}, attempts=2
        )
    except httpx.HTTPStatusError as exc:
        raise ViewerTokenError(f"Twitch refused the device flow: {_message(exc.response)}") from exc
    return resp.json()  # type: ignore[no-any-return]  # device_code, user_code, verification_uri, expires_in, interval


async def poll_device(settings: Settings, device_code: str) -> dict[str, Any] | None:
    """The stored token once the broadcaster has entered the code, None while Twitch still waits for
    them. Raises ViewerTokenError when the code expired or was refused."""
    try:
        resp = await http.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": settings.twitch_viewer_client_id,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT,
                "scopes": "",
            },
            attempts=1,
        )
    except httpx.HTTPStatusError as exc:
        message = _message(exc.response)
        if exc.response.status_code == 400 and message in ("authorization_pending", "slow_down"):
            return None
        raise ViewerTokenError(f"Twitch ended the link: {message}") from exc
    data = resp.json()
    return await store(
        data["access_token"],
        refresh_token=data.get("refresh_token"),
        method="device",
        expires_in=data.get("expires_in"),
    )


async def audit_link(
    actor: Actor, before: dict[str, Any] | None, after: dict[str, Any] | None, *, error: str | None = None
) -> None:
    """``twitch_viewer.link``: an account linked through the device flow, or a link that failed. (Pasting
    a token and unlinking are audited as their admin requests.)"""
    await audit.write(
        AuditEntry(
            "twitch_viewer.link",
            actor,
            AUDIT_TARGET,
            outcome="failed" if error else "ok",
            before=before,
            after=after,
            detail={"error": error} if error else None,
        )
    )


# ── Use ───────────────────────────────────────────────────────────────────


@dataclass
class _Cached:
    token: dict[str, Any] | None
    at: float


class TwitchViewer:
    """The linked account's token for the live playback request (``auth``), refreshed before it
    expires, and its status for the dashboard (``check``)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cached: _Cached | None = None
        self._lock = asyncio.Lock()
        self.last_check: dict[str, Any] | None = None

    def forget(self) -> None:
        """Drop the cached token, after the admin routes change it."""
        self._cached = None
        self.last_check = None

    async def _token(self) -> dict[str, Any] | None:
        if self._cached is None or time.monotonic() - self._cached.at > CACHE_SECONDS:
            self._cached = _Cached(await load_token(), time.monotonic())
        return self._cached.token

    async def _refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await http.request(
                "POST",
                TOKEN_URL,
                data={
                    "client_id": token["clientId"],
                    "grant_type": "refresh_token",
                    "refresh_token": token["refreshToken"],
                },
                attempts=2,
            )
        except httpx.HTTPStatusError as exc:
            raise ViewerTokenError(f"Twitch refused the refresh: {_message(exc.response)}") from exc
        data = resp.json()
        refreshed = {
            **token,
            "token": data["access_token"],
            "refreshToken": data.get("refresh_token") or token["refreshToken"],
            "expiresAt": _expires_at(data.get("expires_in")),
        }
        await save_token(refreshed)
        self._cached = _Cached(refreshed, time.monotonic())
        log.info("refreshed the Twitch viewer token of %s", token.get("login"))
        return refreshed

    async def auth(self) -> ViewerAuth | None:
        """What to send with the live playback request, or None to ask anonymously (nothing linked,
        or a token that expired and could not be refreshed)."""
        async with self._lock:
            token = await self._token()
            if not token or not token.get("token"):
                return None
            expires = token.get("expiresAt")
            left = (dt.datetime.fromisoformat(expires) - dt.datetime.now(dt.UTC)).total_seconds() if expires else None
            if left is not None and left < REFRESH_MARGIN:
                if not token.get("refreshToken"):
                    if left <= 0:
                        log.warning("the Twitch viewer token of %s has expired; link it again", token.get("login"))
                        return None
                else:
                    try:
                        token = await self._refresh(token)
                    except Exception as exc:
                        log.warning("could not refresh the Twitch viewer token of %s: %s", token.get("login"), exc)
                        if left <= 0:
                            return None
            return ViewerAuth(client_id=token["clientId"], token=token["token"], login=token.get("login") or "")

    async def check(self) -> dict[str, Any]:
        """The linked account and whether Twitch still accepts its token."""
        token = await self._token()
        result: dict[str, Any] = {"linked": bool(token), "valid": False, "error": None, **(token_summary(token) or {})}
        result.pop("token", None)
        if token:
            auth = await self.auth()
            try:
                if auth is None:
                    raise ViewerTokenError("The token has expired; link the account again")
                await validate(auth.token)
                result["valid"] = True
            except Exception as exc:
                result["error"] = str(exc)
        self.last_check = {**result, "checkedAt": dt.datetime.now(dt.UTC)}
        return self.last_check

    async def cached_check(self, max_age: float = 600) -> dict[str, Any]:
        last = self.last_check
        if last is None or (dt.datetime.now(dt.UTC) - last["checkedAt"]).total_seconds() > max_age:
            return await self.check()
        return last
