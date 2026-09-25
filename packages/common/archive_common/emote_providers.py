"""7TV, BTTV and FFZ emote APIs: endpoints and response parsing, shared by the
worker (emotes saved with each VOD) and archive-api (GET /v1/emotes/third-party).

Parsers are lenient: a missing or malformed part of a response yields no emotes
rather than an error. Each returns ``{"id", "code"}`` dicts (7TV adds ``flags``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

SEVENTV = "https://7tv.io/v3"
BTTV = "https://api.betterttv.net/3"
FFZ = "https://api.frankerfacez.com/v1"

PROVIDERS = ("7tv", "bttv", "ffz")

Parser = Callable[[Any], list[dict[str, Any]]]
Endpoint = tuple[str, Parser]  # (url, parser)


def _emotes(items: Iterable[Any], code_key: str, *extra: str) -> list[dict[str, Any]]:
    out = []
    for e in items:
        if not isinstance(e, dict) or e.get("id") is None or not e.get(code_key):
            continue
        out.append({"id": e["id"], "code": e[code_key], **{k: e.get(k) for k in extra}})
    return out


def _seventv_set(emote_set: Any) -> list[dict[str, Any]]:
    return _emotes((emote_set or {}).get("emotes") or [], "name", "flags")


def seventv_global(data: Any) -> list[dict[str, Any]]:
    return _seventv_set(data)


def seventv_channel(data: Any) -> list[dict[str, Any]]:
    return _seventv_set((data or {}).get("emote_set"))


def bttv_global(data: Any) -> list[dict[str, Any]]:
    return _emotes(data or [], "code")


def bttv_channel(data: Any) -> list[dict[str, Any]]:
    data = data or {}
    return _emotes((data.get("channelEmotes") or []) + (data.get("sharedEmotes") or []), "code")


def _ffz_sets(data: Any, set_ids: Iterable[Any]) -> list[dict[str, Any]]:
    sets = (data or {}).get("sets") or {}
    return [e for set_id in set_ids for e in _emotes((sets.get(str(set_id)) or {}).get("emoticons") or [], "name")]


def ffz_global(data: Any) -> list[dict[str, Any]]:
    return _ffz_sets(data, (data or {}).get("default_sets") or [])


def ffz_channel(data: Any) -> list[dict[str, Any]]:
    room_set = ((data or {}).get("room") or {}).get("set")
    return _ffz_sets(data, [room_set] if room_set is not None else [])


GLOBAL: dict[str, Endpoint] = {
    "7tv": (f"{SEVENTV}/emote-sets/global", seventv_global),
    "bttv": (f"{BTTV}/cached/emotes/global", bttv_global),
    "ffz": (f"{FFZ}/set/global", ffz_global),
}


def channel(twitch_id: str) -> dict[str, Endpoint]:
    return {
        "7tv": (f"{SEVENTV}/users/twitch/{twitch_id}", seventv_channel),
        "bttv": (f"{BTTV}/cached/users/twitch/{twitch_id}", bttv_channel),
        "ffz": (f"{FFZ}/room/id/{twitch_id}", ffz_channel),
    }
