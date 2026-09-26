"""GET /v1/emotes/third-party: the channel's 7TV, BTTV and FFZ emotes (global + channel).

Fetched server-side from the providers' official APIs so viewers don't each
make those requests. Only ids and codes are returned; clients build image URLs
from the providers' CDNs. A provider that fails is listed in ``failed`` and
its list holds whatever part of it did load.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from archive_common import emote_providers as providers
from archive_common import http

log = logging.getLogger(__name__)


async def _get(url: str) -> Any:
    """JSON body, or None when the provider has no such channel (404)."""
    try:
        return (await http.request("GET", url, attempts=2, max_wait=2.0)).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _parts(twitch_id: str) -> dict[str, list[providers.Endpoint]]:
    """Global first, then channel (a channel emote wins over a global one with the same code)."""
    channel = providers.channel(twitch_id) if twitch_id else {}
    return {p: [providers.GLOBAL[p]] + ([channel[p]] if channel else []) for p in providers.PROVIDERS}


async def _fetch_part(url: str, parse: providers.Parser) -> list[tuple[str, str]]:
    return [(str(e["id"]), str(e["code"])) for e in parse(await _get(url))]


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
