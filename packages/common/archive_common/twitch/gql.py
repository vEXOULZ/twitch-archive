"""Twitch private GQL (gql.twitch.tv): playback tokens, chapters, chat replay."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

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
    # The account the token was asked for as (an ad-free viewer), or None when asked anonymously.
    viewer: str | None = None


@dataclass(frozen=True)
class ViewerAuth:
    """A Twitch user token and the client id it was issued to (GQL refuses the pair mismatched)."""

    client_id: str
    token: str
    login: str


class Gql:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # The linked account for live playback tokens (the worker's TwitchViewer.auth), if any.
        self.viewer: Callable[[], Awaitable[ViewerAuth | None]] | None = None

    async def post(
        self, body: dict[str, Any], *, backup_client: bool = False, auth: ViewerAuth | None = None
    ) -> dict[str, Any]:
        client_id = self.settings.gql_backup_client_id if backup_client else self.settings.gql_client_id
        headers = {"Client-Id": client_id, "Accept": "*/*", "Content-Type": "text/plain;charset=UTF-8"}
        if auth is not None:
            headers["Client-Id"] = auth.client_id
            headers["Authorization"] = f"OAuth {auth.token}"
        resp = await http.request("POST", GQL_URL, json=body, headers=headers)
        data = resp.json()
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            msgs = "; ".join(str(e.get("message", e)) for e in errors)
            raise GqlError(f"{body.get('operationName')}: {msgs}")
        return data  # type: ignore[no-any-return]

    def _persisted(self, operation: str, sha: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "operationName": operation,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
        }

    # ── Playback tokens ───────────────────────────────────────────────────

    async def _playback_token(
        self, *, is_live: bool, login: str = "", vod_id: str = "", auth: ViewerAuth | None = None
    ) -> dict[str, Any] | None:
        data = await self.post(
            self._persisted(
                "PlaybackAccessToken",
                self.settings.gql_hash_playback_token,
                {
                    "isLive": is_live,
                    "isVod": not is_live,
                    "login": login,
                    "platform": "web",
                    "playerType": "site",
                    "vodID": vod_id,
                },
            ),
            auth=auth,
        )
        key = "streamPlaybackAccessToken" if is_live else "videoPlaybackAccessToken"
        return (data.get("data") or {}).get(key)

    async def vod_access_token(self, vod_id: str) -> AccessToken:
        tok = await self._playback_token(is_live=False, vod_id=vod_id)
        if not tok:
            raise GqlError(f"No VOD playback token for {vod_id} (deleted or sub-only?)")
        return AccessToken(tok["value"], tok["signature"])

    async def live_access_token(self, login: str) -> AccessToken:
        """As the linked viewer account when there is one (no stitched ads for Turbo, or a sub where the
        channel gives subs ad-free viewing), falling back to anonymous if that fails."""
        auth = await self._viewer_auth()
        if auth is not None:
            try:
                tok = await self._playback_token(is_live=True, login=login, auth=auth)
                if tok:
                    return AccessToken(tok["value"], tok["signature"], viewer=auth.login)
            except (GqlError, httpx.HTTPStatusError) as exc:
                log.warning("live playback token as %s failed (%s); asking anonymously", auth.login, exc)
        tok = await self._playback_token(is_live=True, login=login)
        if not tok:
            raise GqlError(f"No live playback token for {login} (offline?)")
        return AccessToken(tok["value"], tok["signature"])

    async def _viewer_auth(self) -> ViewerAuth | None:
        if self.viewer is None:
            return None
        try:
            return await self.viewer()
        except Exception as exc:  # the database, a refresh: never a reason not to record
            log.warning("could not load the Twitch viewer token: %s", exc)
            return None

    # ── Chapters ──────────────────────────────────────────────────────────

    async def video_moments(self, vod_id: str) -> list[dict[str, Any]] | None:
        data = await self.post(
            self._persisted("VideoPreviewCard__VideoMoments", self.settings.gql_hash_moments, {"videoId": vod_id}),
            backup_client=True,
        )
        video = (data.get("data") or {}).get("video") or {}
        moments = video.get("moments")
        if moments is None:
            return None
        return moments.get("edges") or []

    async def video_game(self, vod_id: str) -> dict[str, Any] | None:
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

    async def comments(
        self, vod_id: str, *, offset: int | None = None, cursor: str | None = None
    ) -> dict[str, Any] | None:
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
