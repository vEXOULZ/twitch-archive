"""doomtp-bot's chat log API (``GET /api/v2/channels/{login}/log``, ADR-0025 and ADR-0027 in that repo).

Without ``doomtp_api_key`` the channel's public log is read: no moderation entries and
removed messages left out. With a read-scope key both are included.

v2 gives times as ISO 8601; what this client hands back keeps v1's epoch ms (entries' times,
a follow's ``followed_at``, coverage sessions, and gaps as ``from``/``to``), the shape
``bot_logs.data`` and ``vods.bot_chat`` have always stored.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

from archive_common import http
from archive_common.config import Settings

PAGE_LIMIT = 500
_TIMES = ("at", "received_at", "deleted_at", "cleared_at")


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _ms(value: Any) -> Any:
    """An ISO 8601 time as epoch ms; anything else as it is."""
    if not isinstance(value, str):
        return value
    when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return round(when.timestamp() * 1000)


def v1_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A v2 log entry with v1's ms times."""
    out = {**entry, **{k: _ms(entry[k]) for k in _TIMES if k in entry}}
    payload = entry.get("payload")
    if isinstance(payload, dict) and "followed_at" in payload:
        out["payload"] = {**payload, "followed_at": _ms(payload["followed_at"])}
    return out


def v1_coverage(body: dict[str, Any]) -> dict[str, Any]:
    """v2's coverage in v1's shape: ms times, gaps as ``from``/``to``."""
    out = {k: _ms(body[k]) for k in ("since", "until") if k in body}
    if "sessions" in body:
        out["sessions"] = [{**s, "started_at": _ms(s.get("started_at")), "ended_at": _ms(s.get("ended_at"))}
                           for s in body["sessions"]]
    if "gaps" in body:
        out["gaps"] = [{"from": _ms(g.get("start")), "to": _ms(g.get("end")),
                        **{k: v for k, v in g.items() if k not in ("start", "end")}} for g in body["gaps"]]
    if "complete" in body:
        out["complete"] = body["complete"]
    return out


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
        return f"{self.settings.doomtp_url.rstrip('/')}/api/v2/channels/{login}/log{suffix}"

    def _headers(self) -> dict[str, str]:
        key = self.settings.doomtp_api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def log(self, since_ms: int, until_ms: int) -> AsyncIterator[dict[str, Any]]:
        """Entries with ``since_ms <= at < until_ms``, oldest first, in v1's shape."""
        params: dict[str, Any] = {"since": _iso(since_ms), "until": _iso(until_ms), "order": "asc",
                                  "limit": PAGE_LIMIT}
        while True:
            body = (await http.request("GET", self._url(), params=params, headers=self._headers())).json()
            for entry in body.get("items") or []:
                yield v1_entry(entry)
            nxt = body.get("next_cursor")
            if not nxt:
                return
            params = {**params, "cursor": nxt}

    async def coverage(self, since_ms: int, until_ms: int) -> dict[str, Any]:
        """When the bot was listening between the two times, and the gaps, in v1's shape."""
        resp = await http.request("GET", self._url("/coverage"),
                                  params={"since": _iso(since_ms), "until": _iso(until_ms)}, headers=self._headers())
        return v1_coverage(resp.json())
