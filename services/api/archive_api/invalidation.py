"""Drop cached responses as soon as a VOD (or its games) changes.

Database triggers (migration 0006) send ``NOTIFY vods_changed, '<vod id>'`` when
any write to ``vods`` or ``games`` commits; this LISTENs on its own connection. While the connection is down nothing is heard, so every
(re)connect clears the caches outright.

A merge or split also moves the VOD's chat rows and emotes; every one writes the
``vods`` rows too, so the same notice drops the chat replay and emotes cached for it.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from sqlalchemy.engine import make_url

from archive_common.db import VOD_CHANGED

from .comments import Comments
from .middleware import ResponseCache

log = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0
PING_INTERVAL = 60.0  # a dead connection is only noticed when it is used

# Cached responses that embed VODs, besides /vods/{id} itself.
_LIST_PREFIXES = ("vods?", "games?", "games/", "v1/games-played")


def asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy URL (``postgresql+asyncpg://...``) -> plain libpq DSN for asyncpg."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _emotes_of(vod_id: str, key: str) -> bool:
    """``emotes/<id>`` or an ``emotes?`` query naming the VOD (a stray match only costs a re-render)."""
    return key == f"emotes/{vod_id}" or (key.startswith("emotes?") and vod_id in key)


class VodInvalidator:
    def __init__(self, database_url: str, service_cache: ResponseCache, *other_caches: ResponseCache,
                 comments: Comments | None = None) -> None:
        self.dsn = asyncpg_dsn(database_url)
        self.service_cache = service_cache
        self.other_caches = other_caches  # small ones (e.g. /v1/status): cleared on any change
        self.comments = comments

    def invalidate(self, vod_id: str) -> None:
        own = f"vods/{vod_id}"
        self.service_cache.invalidate(
            lambda key: key == own or key.startswith(_LIST_PREFIXES) or _emotes_of(vod_id, key))
        for cache in self.other_caches:
            cache.clear()
        if self.comments is not None:
            self.comments.invalidate(vod_id)

    def clear_all(self) -> None:
        for cache in (self.service_cache, *self.other_caches):
            cache.clear()
        if self.comments is not None:
            for cache in (self.comments.cache, self.comments.long_cache):
                cache.clear()

    def _on_notify(self, _conn, _pid: int, _channel: str, payload: str) -> None:
        log.debug("vod %s changed; dropping cached responses", payload)
        self.invalidate(payload)

    async def run_forever(self) -> None:
        while True:
            conn = None
            try:
                conn = await asyncpg.connect(self.dsn)
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _c: lost.set())
                await conn.add_listener(VOD_CHANGED, self._on_notify)
                self.clear_all()  # anything edited while we were not listening
                while not lost.is_set():
                    try:
                        await asyncio.wait_for(lost.wait(), PING_INTERVAL)
                    except TimeoutError:
                        await conn.execute("select 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("cache invalidation listener: %s; retrying in %.0fs", exc, RECONNECT_DELAY)
            finally:
                if conn is not None and not conn.is_closed():
                    await asyncio.shield(conn.close())
            await asyncio.sleep(RECONNECT_DELAY)
