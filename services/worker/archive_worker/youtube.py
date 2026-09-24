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
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlalchemy.dialects.postgresql import insert

from archive_common import http
from archive_common.config import Settings
from archive_common.db import execute, get_sessionmaker
from archive_common.models import AppState

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
STATE_KEY = "youtube_oauth"
CHUNK = 64 * 1024 * 1024
RETRIABLE_STATUS = {500, 502, 503, 504}


class YouTubeNotAuthorized(RuntimeError):
    pass


async def load_token() -> dict | None:
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
        return row.value if row else None


async def save_token(value: dict) -> None:
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


async def exchange_code(settings: Settings, code: str) -> dict:
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
    token = {"refresh_token": data["refresh_token"], "token": data.get("access_token"), "scopes": SCOPES}
    await save_token(token)
    return token


# ── API client ────────────────────────────────────────────────────────────


class YouTube:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._creds: Credentials | None = None

    async def _credentials(self) -> Credentials:
        if self._creds is None:
            token = await load_token()
            if not token or not token.get("refresh_token"):
                raise YouTubeNotAuthorized(
                    "No YouTube token stored. Run the OAuth flow (GET /admin/youtube/auth) "
                    "or `archive-worker import-youtube-token`."
                )
            self._creds = Credentials(
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

    async def _service(self):
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
        if creds.refresh_token and creds.refresh_token != stored_refresh:
            # Google rarely rotates refresh tokens, but keep the new one if it does.
            token = await load_token() or {}
            await save_token({**token, "refresh_token": creds.refresh_token, "token": creds.token})
        expiry = creds.expiry.replace(tzinfo=dt.timezone.utc).isoformat() if creds.expiry else None
        return {"authorized": True, "valid": True, "accessTokenExpiry": expiry}

    async def keepalive(self, interval_hours: float) -> None:
        """Refresh the token every ``interval_hours`` forever (never returns)."""
        while True:
            result = await self.check()
            if result["valid"]:
                log.info("YouTube token refreshed (keep-alive)")
            else:
                log.error(
                    "YouTube token is not usable (%s). Uploads will fail until the OAuth flow is "
                    "run again: GET /admin/youtube/auth (README section 3).",
                    result["error"],
                )
            await asyncio.sleep(interval_hours * 3600)

    async def upload(
        self,
        path: Path,
        *,
        title: str,
        description: str,
        privacy_status: str,
        category_id: str = "20",
    ) -> dict[str, Any]:
        service = await self._service()
        body = {
            "snippet": {"title": title, "description": description, "categoryId": category_id},
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(path), chunksize=CHUNK, resumable=True, mimetype="video/mp4")
        request = service.videos().insert(
            part="id,snippet,status", body=body, media_body=media, notifySubscribers=True
        )
        return await asyncio.to_thread(self._resumable, request, path)

    def _resumable(self, request, path: Path) -> dict[str, Any]:
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
                retries = 0
            except HttpError as exc:
                if exc.resp.status not in RETRIABLE_STATUS:
                    raise
                retries = self._backoff(retries, exc)
            except (OSError, TimeoutError) as exc:
                retries = self._backoff(retries, exc)
        return response

    @staticmethod
    def _backoff(retries: int, exc: BaseException) -> int:
        retries += 1
        if retries > 10:
            raise exc
        delay = min(300, 2**retries) + random.random()
        log.warning("upload chunk failed (%s); retry %d in %.0fs", exc, retries, delay)
        time.sleep(delay)
        return retries

    async def get_snippet(self, video_id: str, service=None) -> dict | None:
        service = service or await self._service()
        resp = await asyncio.to_thread(service.videos().list(part="snippet", id=video_id).execute)
        items = resp.get("items") or []
        return items[0]["snippet"] if items else None

    async def update_description(self, video_id: str, description: str) -> None:
        service = await self._service()
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


async def token_status() -> dict:
    token = await load_token()
    return {"authorized": bool(token and token.get("refresh_token"))}

