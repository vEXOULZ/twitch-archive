"""GET /v1/emotes/third-party: the channel's 7TV, BTTV and FFZ emotes (global + channel).

Fetched server-side from the providers' official APIs so viewers don't each
make those requests. Only ids and codes are returned; clients build image URLs
from the providers' CDNs. A provider that fails is listed in ``failed`` and
its list holds whatever part of it did load.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from archive_common import http

log = logging.getLogger(__name__)

SEVENTV = "https://7tv.io/v3"
BTTV = "https://api.betterttv.net/3"
FFZ = "https://api.frankerfacez.com/v1"

Pairs = Iterable[tuple[Any, Any]]  # (id, code)


async def _get(url: str) -> Any:
    """JSON body, or None when the provider has no such channel (404)."""
    try:
        return (await http.request("GET", url, attempts=2, max_wait=2.0)).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


# ── Response parsing (per provider: global set, channel set) ───────────────


def _seventv_set(emote_set: Any) -> Pairs:
    return ((e.get("id"), e.get("name")) for e in (emote_set or {}).get("emotes") or [])


def seventv_global(data: Any) -> Pairs:
    return _seventv_set(data)


def seventv_channel(data: Any) -> Pairs:
    return _seventv_set((data or {}).get("emote_set"))


def bttv_global(data: Any) -> Pairs:
    return ((e.get("id"), e.get("code")) for e in data or [])


def bttv_channel(data: Any) -> Pairs:
    data = data or {}
    return ((e.get("id"), e.get("code")) for e in (data.get("channelEmotes") or []) + (data.get("sharedEmotes") or []))


def _ffz_sets(data: Any, set_ids: Iterable[Any]) -> Pairs:
    sets = (data or {}).get("sets") or {}
    for set_id in set_ids:
        for e in (sets.get(str(set_id)) or {}).get("emoticons") or []:
            yield e.get("id"), e.get("name")


def ffz_global(data: Any) -> Pairs:
    return _ffz_sets(data, (data or {}).get("default_sets") or [])


def ffz_channel(data: Any) -> Pairs:
    room_set = ((data or {}).get("room") or {}).get("set")
    return _ffz_sets(data, [room_set] if room_set is not None else [])


# ── Fetching ──────────────────────────────────────────────────────────────

Part = tuple[str, Callable[[Any], Pairs]]  # (url, parser)


def _parts(twitch_id: str) -> dict[str, list[Part]]:
    """Global first, then channel (a channel emote wins over a global one with the same code)."""
    channel = bool(twitch_id)
    return {
        "7tv": [(f"{SEVENTV}/emote-sets/global", seventv_global)]
        + ([(f"{SEVENTV}/users/twitch/{twitch_id}", seventv_channel)] if channel else []),
        "bttv": [(f"{BTTV}/cached/emotes/global", bttv_global)]
        + ([(f"{BTTV}/cached/users/twitch/{twitch_id}", bttv_channel)] if channel else []),
        "ffz": [(f"{FFZ}/set/global", ffz_global)]
        + ([(f"{FFZ}/room/id/{twitch_id}", ffz_channel)] if channel else []),
    }


async def _fetch_part(url: str, parse: Callable[[Any], Pairs]) -> list[tuple[str, str]]:
    return [(str(i), str(c)) for i, c in parse(await _get(url)) if i is not None and c]


async def fetch_third_party_emotes(twitch_id: str) -> dict[str, Any]:
    parts = _parts(twitch_id)
    flat = [(provider, url, parse) for provider, ps in parts.items() for url, parse in ps]
    results = await asyncio.gather(*(_fetch_part(url, parse) for _, url, parse in flat), return_exceptions=True)

    by_code: dict[str, dict[str, dict]] = {p: {} for p in parts}
    failed: list[str] = []
    for (provider, url, _), result in zip(flat, results):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            log.warning("third-party emotes: %s failed: %s", url, result)
            if provider not in failed:
                failed.append(provider)
            continue
        for emote_id, code in result:
            by_code[provider][code] = {"id": emote_id, "code": code, "provider": provider}
    return {**{p: list(emotes.values()) for p, emotes in by_code.items()}, "failed": failed}
