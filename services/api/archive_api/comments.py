"""GET /v1/vods/{vodId}/comments — chat replay.

A straight port of the legacy ``src/middleware/logs.js`` so the frontend's
paging keeps working unchanged:

* ``?content_offset_seconds=`` finds the first comment at/after the offset and
  returns the 200-comment bucket (aligned on ``_id`` relative to the vod's first
  comment) that contains it.
* ``?cursor=`` continues from a base64 JSON cursor
  ``{"id": _id, "content_offset_seconds": n, "createdAt": iso}``.
* 201 rows are fetched; the 201st only exists to build the next cursor.

Two sources, in separate tables: doomtp-bot's chat (``bot_logs``: redeems, notices,
removals…) and the Twitch VOD replay (``logs``). ``?source=auto`` (the default) serves
the bot's when the VOD has it (about as many rows as the replay, so a partial bot log
never hides a full replay); ``replay`` or ``bot`` force one. A bot page's cursor carries
``"src": "bot"``, and a cursor's source wins, so paging never switches source halfway.
Every row says which it came from in ``source``.

A VOD merged into another has no rows of its own any more (they were re-keyed to
that VOD), so it answers every request with an empty page.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, Row, Table, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from archive_common.models import BotLog, Log, Vod
from archive_common.serialize import BOT_LOGS, LOGS, Resource, js_iso

from .errors import LegacyError
from .middleware import JsonBody, ResponseCache

log = logging.getLogger(__name__)

PAGE = 200
EMPTY: dict[str, Any] = {"comments": []}
BOT_SHARE = 0.9  # ``auto`` serves the bot's chat once it has this share of the replay's rows
_lt, _bt, _vt = Log.__table__, BotLog.__table__, Vod.__table__


@dataclass(frozen=True)
class _Source:
    name: str
    resource: Resource
    seq: Column  # paging order within a VOD, rising with the offset
    sent: Column  # when the message was sent
    where: tuple = ()  # which of the table's rows are chat

    @property
    def table(self) -> Table:
        return self.resource.table

    @property
    def key(self) -> str:
        """Cache key suffix; the replay's keys stay the legacy ones."""
        return "" if self is REPLAY else f":{self.name}"


REPLAY = _Source("replay", LOGS, _lt.c["_id"], _lt.c.createdAt)
BOT = _Source("bot", BOT_LOGS, _bt.c.seq, _bt.c.at, (_bt.c.kind.in_(("message", "notice")),))
SOURCES = {s.name: s for s in (REPLAY, BOT)}


def _js_to_fixed1(value: float) -> str:
    # JavaScript Number.prototype.toFixed(1) rounds half away from zero
    scaled = math.floor(abs(value) * 10 + 0.5) / 10
    return f"{math.copysign(scaled, value) if value else 0.0:.1f}"


def _encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any] | None:
    # Node's Buffer.from(x, "base64") ignores invalid characters and padding
    cleaned = "".join(ch for ch in cursor.replace("-", "+").replace("_", "/") if ch.isalnum() or ch in "+/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        data = json.loads(base64.b64decode(cleaned).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _page(rows: list[dict], created_at: Any, src: _Source) -> dict[str, Any]:
    out: dict[str, Any] = {"comments": rows[:PAGE]}
    if len(rows) == PAGE + 1:
        nxt = rows[PAGE]
        cursor = {"id": nxt["_id"], "content_offset_seconds": nxt["content_offset_seconds"], "createdAt": created_at}
        if src is not REPLAY:  # replay cursors stay exactly the legacy ones
            cursor["src"] = src.name
        out["cursor"] = _encode_cursor(cursor)
    return out


class Comments:
    def __init__(self, cache: ResponseCache, long_cache: ResponseCache) -> None:
        self.cache = cache  # offset pages and the auto source choice, 5 min
        self.long_cache = long_cache  # cursor pages and starting ids, 24 h

    def invalidate(self, vod_id: str) -> None:
        """Drop everything cached for ``vod_id`` (its chat rows moved, or bot chat was added)."""
        prefixes = (f"offset:{vod_id}:", f"cursor:{vod_id}:", f"start:{vod_id}:")
        singles = (f"start:{vod_id}", f"source:{vod_id}")
        for cache in (self.cache, self.long_cache):
            cache.invalidate(lambda key: key.startswith(prefixes) or key in singles)

    def clear(self) -> None:
        for cache in (self.cache, self.long_cache):
            cache.clear()

    async def handle(
        self, engine: AsyncEngine, vod_id: str, offset_raw: str | None, cursor: str | None,
        source: str | None = None,
    ) -> JsonBody:
        """A connection is only taken on a cache miss."""
        source = source or "auto"
        if source != "auto" and source not in SOURCES:
            raise LegacyError(400, f"source must be one of auto, {', '.join(SOURCES)}")
        offset: float | None = None
        if offset_raw not in (None, ""):
            try:
                offset = float(offset_raw)
            except ValueError:
                pass
            if offset is not None and not math.isfinite(offset):
                offset = None
        if offset is None and not cursor:
            raise LegacyError(400, "Missing request params")

        if offset is not None:
            fixed = _js_to_fixed1(offset)

            seconds = int(float(fixed))  # all the search uses
            src = await self._source(engine, vod_id, source)

            async def by_offset() -> dict:
                # Only pages of existing vods are cached, so a hit skips the vod lookup.
                async with engine.connect() as conn:
                    vod = await self._vod(conn, vod_id)
                    if vod is None:
                        raise LegacyError(500, f"Failed to retrieve vod {vod_id}")
                    if vod.merged_into is not None:
                        return EMPTY
                    result = await self._offset_search(conn, src, vod_id, seconds, vod.createdAt)
                if result is None:
                    raise LegacyError(500, f"Failed to retrieve comments from offset {fixed}")
                return result

            return await self.cache.get_or_render(f"offset:{vod_id}:{seconds}{src.key}", by_offset)

        async def by_cursor() -> dict:
            cursor_json = _decode_cursor(cursor or "")
            if cursor_json is None:
                raise LegacyError(500, "Failed to parse cursor")
            src = SOURCES.get(cursor_json.get("src"), REPLAY)
            async with engine.connect() as conn:
                result = await self._cursor_search(conn, src, vod_id, cursor_json)
                if result is None and (vod := await self._vod(conn, vod_id)) and vod.merged_into is not None:
                    return EMPTY
            if result is None:
                raise LegacyError(500, f"Failed to retrieve comments from cursor {cursor}")
            return result

        # The cursor names its source, so the key needs nothing else.
        return await self.long_cache.get_or_render(f"cursor:{vod_id}:{cursor}", by_cursor)

    async def _source(self, engine: AsyncEngine, vod_id: str, source: str) -> _Source:
        if source != "auto":
            return SOURCES[source]
        key = f"source:{vod_id}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        async with engine.connect() as conn:
            bot, replay = (await conn.execute(select(
                select(func.count()).select_from(_bt).where(_bt.c.vod_id == vod_id, *BOT.where).scalar_subquery(),
                select(func.count()).select_from(_lt).where(_lt.c.vod_id == vod_id).scalar_subquery(),
            ))).one()
        src = BOT if bot and bot >= BOT_SHARE * replay else REPLAY
        self.cache.set(key, src)
        return src

    async def _vod(self, conn: AsyncConnection, vod_id: str) -> Row | None:
        return (await conn.execute(select(_vt.c.createdAt, _vt.c.merged_into).where(_vt.c.id == vod_id))).first()

    async def _rows(self, conn: AsyncConnection, src: _Source, *where) -> list[dict]:
        stmt = (
            select(*src.resource.columns())
            .where(*where, *src.where)
            .order_by(src.table.c.content_offset_seconds.asc(), src.seq.asc())
            .limit(PAGE + 1)
        )
        return [src.resource.to_json(r) for r in (await conn.execute(stmt)).mappings()]

    async def _cursor_search(self, conn: AsyncConnection, src: _Source, vod_id: str, cursor: dict) -> dict | None:
        t = src.table
        try:
            seq = int(cursor["id"])
            created = cursor["createdAt"]
            if not isinstance(created, str):
                return None
            where = [
                t.c.vod_id == vod_id,
                src.seq >= seq,
                src.sent >= dt.datetime.fromisoformat(created),
            ]
            # Lets the (vod_id, content_offset_seconds, _id) index seek to the cursor instead of
            # scanning from the start of the VOD. Unlike the legacy API, a comment stored with
            # a later _id but an earlier offset than the cursor is not shown again.
            if cursor.get("content_offset_seconds") is not None:
                where.append(t.c.content_offset_seconds >= math.floor(float(cursor["content_offset_seconds"])))
            rows = await self._rows(conn, src, *where)
        except (KeyError, TypeError, ValueError):
            return None
        if not rows:
            return None
        return _page(rows, created, src)

    async def _offset_search(
        self, conn: AsyncConnection, src: _Source, vod_id: str, offset: int, vod_created
    ) -> dict | None:
        t = src.table
        starting_id = await self._starting_id(conn, src, vod_id, vod_created)
        if starting_id is None:
            return None
        comment_id = (
            await conn.execute(
                select(src.seq)
                .where(t.c.vod_id == vod_id, t.c.content_offset_seconds >= offset, src.sent >= vod_created, *src.where)
                .order_by(t.c.content_offset_seconds.asc(), src.seq.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if comment_id is None:
            return None
        index = (comment_id - starting_id) // PAGE * PAGE
        rows = await self._rows(conn, src, t.c.vod_id == vod_id, src.seq >= starting_id + index)
        if not rows:
            return None
        return _page(rows, js_iso(vod_created), src)

    async def _starting_id(self, conn: AsyncConnection, src: _Source, vod_id: str, vod_created) -> int | None:
        t = src.table
        key = f"start:{vod_id}{src.key}"
        cached = self.long_cache.get(key)
        if cached is not None:
            return cached
        value = (
            await conn.execute(
                select(src.seq)
                .where(t.c.vod_id == vod_id, src.sent >= vod_created, *src.where)
                .order_by(t.c.content_offset_seconds.asc(), src.seq.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if value is not None:
            self.long_cache.set(key, value)
        return value
