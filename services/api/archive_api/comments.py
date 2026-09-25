"""GET /v1/vods/{vodId}/comments — chat replay.

A straight port of the legacy ``src/middleware/logs.js`` so the frontend's
paging keeps working unchanged:

* ``?content_offset_seconds=`` finds the first comment at/after the offset and
  returns the 200-comment bucket (aligned on ``_id`` relative to the vod's first
  comment) that contains it.
* ``?cursor=`` continues from a base64 JSON cursor
  ``{"id": _id, "content_offset_seconds": n, "createdAt": iso}``.
* 201 rows are fetched; the 201st only exists to build the next cursor.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from archive_common.models import Log, Vod
from archive_common.serialize import LOGS, js_iso

from .errors import LegacyError
from .middleware import JsonBody, ResponseCache

log = logging.getLogger(__name__)

PAGE = 200
_lt = Log.__table__
_vt = Vod.__table__


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


def _page(rows: list[dict], created_at: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"comments": rows[:PAGE]}
    if len(rows) == PAGE + 1:
        nxt = rows[PAGE]
        out["cursor"] = _encode_cursor(
            {"id": nxt["_id"], "content_offset_seconds": nxt["content_offset_seconds"], "createdAt": created_at}
        )
    return out


class Comments:
    def __init__(self, cache: ResponseCache, long_cache: ResponseCache) -> None:
        self.cache = cache  # offset pages, 5 min
        self.long_cache = long_cache  # cursor pages and starting ids, 24 h

    async def handle(
        self, conn: AsyncConnection, vod_id: str, offset_raw: str | None, cursor: str | None
    ) -> JsonBody:
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

            async def by_offset() -> dict:
                # Only pages of existing vods are cached, so a hit skips the vod lookup.
                vod_created = (
                    await conn.execute(select(_vt.c.createdAt).where(_vt.c.id == vod_id))
                ).scalar_one_or_none()
                if vod_created is None:
                    raise LegacyError(500, f"Failed to retrieve vod {vod_id}")
                result = await self._offset_search(conn, vod_id, int(float(fixed)), vod_created)
                if result is None:
                    raise LegacyError(500, f"Failed to retrieve comments from offset {fixed}")
                return result

            return await self.cache.get_or_render(f"offset:{vod_id}:{fixed}", by_offset)

        async def by_cursor() -> dict:
            cursor_json = _decode_cursor(cursor or "")
            if cursor_json is None:
                raise LegacyError(500, "Failed to parse cursor")
            result = await self._cursor_search(conn, vod_id, cursor_json)
            if result is None:
                raise LegacyError(500, f"Failed to retrieve comments from cursor {cursor}")
            return result

        return await self.long_cache.get_or_render(f"cursor:{vod_id}:{cursor}", by_cursor)

    async def _rows(self, conn: AsyncConnection, *where) -> list[dict]:
        stmt = (
            select(*LOGS.columns())
            .where(*where)
            .order_by(_lt.c.content_offset_seconds.asc(), _lt.c["_id"].asc())
            .limit(PAGE + 1)
        )
        return [LOGS.to_json(r) for r in (await conn.execute(stmt)).mappings()]

    async def _cursor_search(self, conn: AsyncConnection, vod_id: str, cursor: dict) -> dict | None:
        try:
            seq = int(cursor["id"])
            created = cursor["createdAt"]
            if not isinstance(created, str):
                return None
            rows = await self._rows(
                conn,
                _lt.c.vod_id == vod_id,
                _lt.c["_id"] >= seq,
                _lt.c.createdAt >= dt.datetime.fromisoformat(created),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not rows:
            return None
        return _page(rows, created)

    async def _offset_search(self, conn: AsyncConnection, vod_id: str, offset: int, vod_created) -> dict | None:
        starting_id = await self._starting_id(conn, vod_id, vod_created)
        if starting_id is None:
            return None
        comment_id = (
            await conn.execute(
                select(_lt.c["_id"])
                .where(
                    _lt.c.vod_id == vod_id,
                    _lt.c.content_offset_seconds >= offset,
                    _lt.c.createdAt >= vod_created,
                )
                .order_by(_lt.c.content_offset_seconds.asc(), _lt.c["_id"].asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if comment_id is None:
            return None
        index = (comment_id - starting_id) // PAGE * PAGE
        rows = await self._rows(conn, _lt.c.vod_id == vod_id, _lt.c["_id"] >= starting_id + index)
        if not rows:
            return None
        return _page(rows, js_iso(vod_created))

    async def _starting_id(self, conn: AsyncConnection, vod_id: str, vod_created) -> int | None:
        key = f"start:{vod_id}"
        cached = self.long_cache.get(key)
        if cached is not None:
            return cached
        value = (
            await conn.execute(
                select(_lt.c["_id"])
                .where(_lt.c.vod_id == vod_id, _lt.c.createdAt >= vod_created)
                .order_by(_lt.c.content_offset_seconds.asc(), _lt.c["_id"].asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if value is not None:
            self.long_cache.set(key, value)
        return value
