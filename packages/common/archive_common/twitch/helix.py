"""Twitch Helix API with an app (client-credentials) token held in memory."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from cachetools import TTLCache

from .. import http
from ..config import Settings, get_settings

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


class Helix:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._games: TTLCache[str, dict | None] = TTLCache(maxsize=1000, ttl=24 * 3600)

    @property
    def configured(self) -> bool:
        return bool(self.settings.twitch_client_id and self.settings.twitch_client_secret.get_secret_value())

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.monotonic() < self._expires_at:
                return self._token
            resp = await http.request(
                "POST",
                TOKEN_URL,
                params={
                    "client_id": self.settings.twitch_client_id,
                    "client_secret": self.settings.twitch_client_secret.get_secret_value(),
                    "grant_type": "client_credentials",
                },
            )
            data = resp.json()
            self._token = data["access_token"]
            # refresh a little early
            self._expires_at = time.monotonic() + max(60, int(data.get("expires_in", 3600)) - 300)
            log.info("Obtained Twitch app access token")
            return self._token

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        for force in (False, True):
            token = await self._get_token(force=force)
            try:
                resp = await http.request(
                    "GET",
                    f"{HELIX}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Client-Id": self.settings.twitch_client_id},
                )
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401 and not force:
                    log.info("Helix returned 401, refreshing app token")
                    continue
                raise
        raise AssertionError("unreachable")

    # ── Convenience wrappers ──────────────────────────────────────────────

    async def get_stream(self, user_id: str) -> dict | None:
        data = await self.get("/streams", {"user_id": user_id})
        items = data.get("data") or []
        return items[0] if items else None

    async def get_video(self, video_id: str) -> dict | None:
        """Return the Helix video or None when it no longer exists."""
        try:
            data = await self.get("/videos", {"id": video_id})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                return None
            raise
        items = data.get("data") or []
        return items[0] if items else None

    async def list_videos(self, user_id: str, video_type: str = "archive", first: int = 20) -> list[dict]:
        data = await self.get("/videos", {"user_id": user_id, "type": video_type, "first": first})
        return data.get("data") or []

    async def video_for_stream(self, user_id: str, stream_id: str) -> dict | None:
        """The archive video recorded from ``stream_id``, if Twitch has made one yet."""
        return next((v for v in await self.list_videos(user_id) if str(v.get("stream_id")) == stream_id), None)

    async def get_game(self, game_id: str) -> dict | None:
        if game_id in self._games:
            return self._games[game_id]
        data = await self.get("/games", {"id": game_id})
        items = data.get("data") or []
        game = items[0] if items else None
        self._games[game_id] = game
        return game

    async def channel_badges(self, broadcaster_id: str) -> list[dict] | None:
        data = await self.get("/chat/badges", {"broadcaster_id": broadcaster_id})
        return data.get("data")

    async def global_badges(self) -> list[dict] | None:
        data = await self.get("/chat/badges/global")
        return data.get("data")

