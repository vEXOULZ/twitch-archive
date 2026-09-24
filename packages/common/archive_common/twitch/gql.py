"""Twitch private GQL (gql.twitch.tv): playback tokens, chapters, chat replay."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .. import http
from ..config import Settings, get_settings

log = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"


class GqlError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessToken:
    value: str
    signature: str


class Gql:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def post(self, body: dict[str, Any], *, backup_client: bool = False) -> dict:
        client_id = self.settings.gql_backup_client_id if backup_client else self.settings.gql_client_id
        resp = await http.request(
            "POST",
            GQL_URL,
            json=body,
            headers={"Client-Id": client_id, "Accept": "*/*", "Content-Type": "text/plain;charset=UTF-8"},
        )
        data = resp.json()
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            msgs = "; ".join(str(e.get("message", e)) for e in errors)
            raise GqlError(f"{body.get('operationName')}: {msgs}")
        return data

    def _persisted(self, operation: str, sha: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "operationName": operation,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
        }

    # ── Playback tokens ───────────────────────────────────────────────────

    async def vod_access_token(self, vod_id: str) -> AccessToken:
        data = await self.post(
            self._persisted(
                "PlaybackAccessToken",
                self.settings.gql_hash_playback_token,
                {
                    "isLive": False,
                    "isVod": True,
                    "login": "",
                    "platform": "web",
                    "playerType": "site",
                    "vodID": vod_id,
                },
            )
        )
        tok = (data.get("data") or {}).get("videoPlaybackAccessToken")
        if not tok:
            raise GqlError(f"No VOD playback token for {vod_id} (deleted or sub-only?)")
        return AccessToken(tok["value"], tok["signature"])

    async def live_access_token(self, login: str) -> AccessToken:
        data = await self.post(
            self._persisted(
                "PlaybackAccessToken",
                self.settings.gql_hash_playback_token,
                {
                    "isLive": True,
                    "isVod": False,
                    "login": login,
                    "platform": "web",
                    "playerType": "site",
                    "vodID": "",
                },
            )
        )
        tok = (data.get("data") or {}).get("streamPlaybackAccessToken")
        if not tok:
            raise GqlError(f"No live playback token for {login} (offline?)")
        return AccessToken(tok["value"], tok["signature"])

    # ── Chapters ──────────────────────────────────────────────────────────

    async def video_moments(self, vod_id: str) -> list[dict] | None:
        data = await self.post(
            self._persisted("VideoPreviewCard__VideoMoments", self.settings.gql_hash_moments, {"videoId": vod_id}),
            backup_client=True,
        )
        video = (data.get("data") or {}).get("video") or {}
        moments = video.get("moments")
        if moments is None:
            return None
        return moments.get("edges") or []

    async def video_game(self, vod_id: str) -> dict | None:
        data = await self.post(
            self._persisted(
                "NielsenContentMetadata",
                self.settings.gql_hash_nielsen,
                {
                    "isCollectionContent": False,
                    "isLiveContent": False,
                    "isVODContent": True,
                    "collectionID": "",
                    "login": "",
                    "vodID": vod_id,
                },
            )
        )
        return (data.get("data") or {}).get("video")

    # ── Chat replay ───────────────────────────────────────────────────────

    async def comments(self, vod_id: str, *, offset: int | None = None, cursor: str | None = None) -> dict | None:
        variables: dict[str, Any] = {"videoID": vod_id}
        if cursor:
            variables["cursor"] = cursor
        else:
            variables["contentOffsetSeconds"] = offset or 0
        data = await self.post(
            self._persisted("VideoCommentsByOffsetOrCursor", self.settings.gql_hash_comments, variables),
            backup_client=bool(cursor),
        )
        return (data.get("data") or {}).get("video")
