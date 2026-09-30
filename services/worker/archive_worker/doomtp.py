"""doomtp-bot's chat log API (``GET /api/v1/channels/{login}/log``, ADR-0025 in that repo).

Without ``doomtp_api_key`` the channel's public log is read: no moderation entries and
removed messages left out. With a read-scope key both are included.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from archive_common import http
from archive_common.config import Settings

PAGE_LIMIT = 500


class Doomtp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.doomtp_url)

    @property
    def keyed(self) -> bool:
        return bool(self.settings.doomtp_api_key.get_secret_value())

    def _url(self, suffix: str = "") -> str:
        login = self.settings.doomtp_login or self.settings.twitch_username
        return f"{self.settings.doomtp_url.rstrip('/')}/api/v1/channels/{login}/log{suffix}"

    def _headers(self) -> dict[str, str]:
        key = self.settings.doomtp_api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def log(self, since_ms: int, until_ms: int) -> AsyncIterator[dict[str, Any]]:
        """Entries with ``since_ms <= at < until_ms``, oldest first."""
        params: dict[str, Any] = {"since": since_ms, "until": until_ms, "order": "asc", "limit": PAGE_LIMIT}
        while True:
            body = (await http.request("GET", self._url(), params=params, headers=self._headers())).json()
            for entry in body.get("entries") or []:
                yield entry
            nxt = body.get("next")
            if not nxt:
                return
            params = {**params, "cursor": nxt}

    async def coverage(self, since_ms: int, until_ms: int) -> dict[str, Any]:
        """When the bot was listening between the two times, and the gaps."""
        resp = await http.request("GET", self._url("/coverage"), params={"since": since_ms, "until": until_ms},
                                  headers=self._headers())
        return resp.json()
