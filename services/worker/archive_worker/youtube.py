"""YouTube Data API v3: OAuth token storage, resumable uploads, description edits.

The OAuth token lives in the ``app_state`` table (key ``youtube_oauth``)
instead of being written back into a config file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from archive_common import http
from archive_common.config import Settings
from archive_common.db import execute, get_sessionmaker
from archive_common.models import AppState
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlalchemy.dialects.postgresql import insert

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
STATE_KEY = "youtube_oauth"
CHUNK = 64 * 1024 * 1024
RETRIABLE_STATUS = {500, 502, 503, 504}


NO_CHANNEL = (
    "This Google account has no YouTube channel. Create one on youtube.com, or connect again and pick the "
    "channel's own (brand) account."
)


class YouTubeNotAuthorized(RuntimeError):
    pass


async def load_token() -> dict[str, Any] | None:
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
        return row.value if row else None


async def save_token(value: dict[str, Any]) -> None:
    stmt = insert(AppState).values(key=STATE_KEY, value=value)
    await execute(stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": value}))


# ── OAuth consent flow (admin endpoints) ──────────────────────────────────


def _sign(settings: Settings, payload: str) -> str:
    key = settings.admin_api_key.get_secret_value().encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


def consent_url(settings: Settings) -> str:
    ts = str(int(time.time()))
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_url,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": f"{ts}.{_sign(settings, ts)}",
    }
    return f"{AUTH_URI}?{urlencode(params)}"


def verify_state(settings: Settings, state: str, max_age: int = 900) -> bool:
    ts, _, sig = state.partition(".")
    if not ts.isdigit() or not hmac.compare_digest(sig, _sign(settings, ts)):
        return False
    return time.time() - int(ts) <= max_age


async def exchange_code(settings: Settings, code: str) -> dict[str, Any]:
    resp = await http.request(
        "POST",
        TOKEN_URI,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "redirect_uri": settings.google_redirect_url,
            "grant_type": "authorization_code",
        },
        attempts=1,
    )
    data = resp.json()
    if "refresh_token" not in data:
        existing = await load_token() or {}
        data["refresh_token"] = existing.get("refresh_token")
    now = dt.datetime.now(dt.UTC)
    # Google sends refresh_token_expires_in only for a time-limited grant; connectedAt dates the token either way.
    lifetime = data.get("refresh_token_expires_in")
    token = {
        "refresh_token": data["refresh_token"],
        "token": data.get("access_token"),
        "scopes": SCOPES,
        "connectedAt": now.isoformat(),
        "refreshTokenExpiresAt": (now + dt.timedelta(seconds=int(lifetime))).isoformat() if lifetime else None,
    }
    await save_token(token)
    return token


# ── API client ────────────────────────────────────────────────────────────


class YouTube:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._creds: Credentials | None = None
        self.last_check: dict[str, Any] | None = None  # check() result plus "checkedAt"
        self._check_lock = asyncio.Lock()

    async def _credentials(self) -> Credentials:
        if self._creds is None:
            token = await load_token()
            if not token or not token.get("refresh_token"):
                raise YouTubeNotAuthorized(
                    "No YouTube token stored. Run the OAuth flow (GET /admin/youtube/auth) "
                    "or `archive-worker import-youtube-token`."
                )
            self._creds = Credentials(  # type: ignore[no-untyped-call]
                token=token.get("token"),
                refresh_token=token["refresh_token"],
                token_uri=TOKEN_URI,
                client_id=self.settings.google_client_id,
                client_secret=self.settings.google_client_secret.get_secret_value(),
                scopes=token.get("scopes"),
            )
        if not self._creds.valid:
            await asyncio.to_thread(self._creds.refresh, GoogleRequest())
        return self._creds

    async def _service(self):  # type: ignore[no-untyped-def]
        # Built per call: the service wraps an httplib2.Http, which is not safe to share
        # across the worker threads that concurrent jobs use.
        creds = await self._credentials()
        return await asyncio.to_thread(build, "youtube", "v3", credentials=creds, cache_discovery=False)

    async def check(self) -> dict[str, Any]:
        """Force a token refresh against Google.

        Proves the stored refresh token still works (a stored token can be
        revoked), and counts as "use" for Google's rule that revokes refresh
        tokens left unused for six months.
        """
        result = await self._check()
        self.last_check = {**result, "checkedAt": dt.datetime.now(dt.UTC)}
        return result

    async def cached_check(self, max_age: float = 600) -> dict[str, Any]:
        """The last check() result (with ``checkedAt``), running a new one if it is older
        than ``max_age`` seconds. Cheap enough to call on every dashboard refresh."""
        async with self._check_lock:
            last = self.last_check
            if last is None or (dt.datetime.now(dt.UTC) - last["checkedAt"]).total_seconds() > max_age:
                await self.check()
            return self.last_check  # type: ignore[return-value]

    async def _check(self) -> dict[str, Any]:
        if not self.settings.google_client_id:
            return {"authorized": False, "valid": False, "error": "ARCHIVE_GOOGLE_CLIENT_ID is not configured"}
        self._creds = None
        try:
            creds = await self._credentials()
            stored_refresh = creds.refresh_token
            await asyncio.to_thread(creds.refresh, GoogleRequest())
        except YouTubeNotAuthorized as exc:
            return {"authorized": False, "valid": False, "error": str(exc)}
        except Exception as exc:  # RefreshError (invalid_grant) or network trouble
            self._creds = None
            return {"authorized": True, "valid": False, "error": f"{type(exc).__name__}: {exc}"}
        token = await load_token() or {}
        if creds.refresh_token and creds.refresh_token != stored_refresh:
            # Google rarely rotates refresh tokens, but keep the new one if it does.
            await save_token({**token, "refresh_token": creds.refresh_token, "token": creds.token})
        expiry = creds.expiry.replace(tzinfo=dt.UTC).isoformat() if creds.expiry else None
        result: dict[str, Any] = {
            "authorized": True,
            "valid": True,
            "accessTokenExpiry": expiry,
            # None for a token imported from the legacy config, which never recorded either.
            "connectedAt": token.get("connectedAt"),
            "refreshTokenExpiresAt": token.get("refreshTokenExpiresAt"),
        }
        # A token that refreshes can still be for an account with no channel, which can't upload.
        try:
            channel = await self.own_channel()
        except Exception as exc:  # quota or network trouble: the token is still fine, so say nothing
            log.warning("YouTube channel lookup failed: %s", exc)
            return result
        if channel is None:
            return {**result, "valid": False, "channel": None, "error": NO_CHANNEL}
        return {**result, "channel": channel}

    async def own_channel(self) -> dict[str, str] | None:
        """The channel uploads go to (the connected account's own), or None if the account has none.
        One quota unit."""
        service = await self._service()  # type: ignore[no-untyped-call]
        try:
            resp = await asyncio.to_thread(service.channels().list(part="snippet", mine=True).execute)
        except HttpError as exc:
            if "youtubeSignupRequired" in str(exc):
                return None
            raise
        items = resp.get("items") or []
        if not items:
            return None
        channel_id = items[0]["id"]
        snippet = items[0].get("snippet") or {}
        handle = snippet.get("customUrl") or ""
        # A handle ("@name") has a URL of its own; an old custom URL may not, so the id is safer then.
        path = handle if handle.startswith("@") else f"channel/{channel_id}"
        return {"id": channel_id, "title": snippet.get("title") or channel_id, "url": f"https://www.youtube.com/{path}"}

    async def keepalive(self) -> None:
        """Refresh the token every ``youtube_keepalive_hours`` while uploads are on (never returns).
        Both are read each time round, so a dashboard change applies from the next refresh."""
        while True:
            if not self.settings.youtube_upload:
                await asyncio.sleep(self.settings.youtube_keepalive_hours * 3600)
                continue
            result = await self.check()
            if result["valid"]:
                log.info("YouTube token refreshed (keep-alive)")
            else:
                log.error(
                    "YouTube token is not usable (%s). Uploads will fail until the OAuth flow is "
                    "run again: GET /admin/youtube/auth (README section 3).",
                    result["error"],
                )
            await asyncio.sleep(self.settings.youtube_keepalive_hours * 3600)

    async def upload(
        self,
        path: Path,
        *,
        title: str,
        description: str,
        privacy_status: str,
        category_id: str = "20",
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """``on_progress(percent)`` is called (from a worker thread) about every 10%."""
        service = await self._service()  # type: ignore[no-untyped-call]
        body = {
            "snippet": {"title": title, "description": description, "categoryId": category_id},
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(path), chunksize=CHUNK, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(part="id,snippet,status", body=body, media_body=media, notifySubscribers=True)
        return await asyncio.to_thread(self._resumable, request, path, on_progress)

    def _resumable(self, request, path: Path, on_progress: Callable[[int], None] | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        response = None
        retries = 0
        last_pct = -10
        while response is None:
            try:
                status, response = request.next_chunk()
                if status is not None:
                    pct = int(status.progress() * 100)
                    if pct >= last_pct + 10:
                        log.info("upload %s: %d%%", path.name, pct)
                        last_pct = pct
                        if on_progress is not None:
                            on_progress(pct)
                retries = 0
            except HttpError as exc:
                if exc.resp.status not in RETRIABLE_STATUS:
                    raise
                retries = self._backoff(retries, exc)
            except (OSError, TimeoutError) as exc:
                retries = self._backoff(retries, exc)
        return response  # type: ignore[no-any-return]

    @staticmethod
    def _backoff(retries: int, exc: BaseException) -> int:
        retries += 1
        if retries > 10:
            raise exc
        delay = min(300, 2**retries) + random.random()
        log.warning("upload chunk failed (%s); retry %d in %.0fs", exc, retries, delay)
        time.sleep(delay)
        return retries

    async def get_snippet(self, video_id: str, service=None) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
        service = service or await self._service()  # type: ignore[no-untyped-call]
        resp = await asyncio.to_thread(service.videos().list(part="snippet", id=video_id).execute)
        items = resp.get("items") or []
        return items[0]["snippet"] if items else None

    async def update_description(self, video_id: str, description: str) -> None:
        service = await self._service()  # type: ignore[no-untyped-call]
        snippet = await self.get_snippet(video_id, service)
        if snippet is None:
            log.warning("YouTube video %s not found; skipping description update", video_id)
            return
        body = {
            "id": video_id,
            "snippet": {
                "title": snippet["title"],
                "description": description,
                "categoryId": snippet.get("categoryId", "20"),
            },
        }
        await asyncio.to_thread(service.videos().update(part="snippet", body=body).execute)


async def import_legacy_token(path: Path) -> None:
    """Import youtube.auth from the legacy config/config.json."""
    cfg = json.loads(path.read_text(encoding="utf-8"))
    auth = (cfg.get("youtube") or {}).get("auth") or {}
    if not auth.get("refresh_token"):
        raise SystemExit(f"{path}: youtube.auth.refresh_token not found")
    # scopes=None: refresh with whatever scopes the original grant had
    await save_token({"refresh_token": auth["refresh_token"], "token": None, "scopes": None})


async def token_status() -> dict[str, Any]:
    token = await load_token()
    return {"authorized": bool(token and token.get("refresh_token"))}
