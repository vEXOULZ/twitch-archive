"""Read-only Feathers-compatible services: /vods, /games, /emotes, /streams."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, and_, func, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from archive_common.config import Settings
from archive_common.serialize import (
    EMOTES, GAMES, STREAMS, VODS, Resource, attach_games, not_hidden, not_merged_away, of_shown_vod, vods_json,
)

from . import feathers_query as fq
from .errors import FeathersError, bad_literal


async def _vods_by_id(conn: AsyncConnection, vod_ids: list[str]) -> dict[str, dict]:
    if not vod_ids:
        return {}
    return {v["id"]: v for v in await vods_json(conn, VODS.table.c.id.in_(vod_ids), not_hidden())}


class Service:
    def __init__(self, resource: Resource, settings: Settings, special: dict[str, fq.Special] | None = None,
                 shown: ColumnElement[bool] | None = None):
        self.resource = resource
        self.settings = settings
        self.special = special or {}
        self.shown = true() if shown is None else shown  # rows that exist as far as find and get go

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        """Associations the legacy include() hooks added."""

    def scope(self, query: dict[str, Any]) -> ColumnElement[bool]:
        """Rows ``find`` considers at all, before the query's own filters."""
        return true()

    async def find(self, conn: AsyncConnection, qs: str) -> dict[str, Any]:
        q = fq.parse(
            self.resource,
            qs,
            default_limit=self.settings.paginate_default,
            max_limit=self.settings.paginate_max,
            special=self.special,
        )
        where = and_(self.shown, self.scope(q.query), q.where)
        total = (await conn.execute(select(func.count()).select_from(self.resource.table).where(where))).scalar_one()
        data: list[dict] = []
        if q.limit > 0:
            stmt = (
                select(*self.resource.columns(q.select))
                .where(where)
                .order_by(*q.order_by)
                .limit(q.limit)
                .offset(q.skip)
            )
            rows = await conn.execute(stmt)
            data = [self.resource.to_json(r, q.select) for r in rows.mappings()]
            await self.embed(conn, data)
        return {"total": total, "limit": q.limit, "skip": q.skip, "data": data}

    async def get(self, conn: AsyncConnection, id_: str) -> dict[str, Any]:
        id_field = self.resource.by_key[self.resource.id_key]
        col = id_field.column
        stmt = select(*self.resource.columns()).where(col == fq.typed_value(col, id_), self.shown)
        try:
            row = (await conn.execute(stmt)).mappings().first()
        except DBAPIError as exc:
            if not bad_literal(exc):  # e.g. /streams/abc is a 404; an outage is not
                raise
            row = None
        if row is None:
            raise FeathersError(404, f"No record found for id '{id_}'")
        item = self.resource.to_json(row)
        await self.embed(conn, [item])
        return item


class VodsService(Service):
    def __init__(self, settings: Settings) -> None:
        super().__init__(VODS, settings, {"chapters": fq.chapter_filter(VODS.table.c.chapters)}, not_hidden())

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        await attach_games(conn, items)

    def scope(self, query: dict[str, Any]) -> ColumnElement[bool]:
        """VODs merged into another one are left out unless ``$merged=true``
        (``GET /vods/{id}`` still answers for them, with ``merged_into``). Hidden VODs are never shown."""
        return true() if query.get("$merged") == "true" else not_merged_away()


class GamesService(Service):
    def __init__(self, settings: Settings) -> None:
        super().__init__(GAMES, settings, shown=of_shown_vod(GAMES.table.c.vod_id))

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        vods = await _vods_by_id(conn, list({i["vodId"] for i in items if "vodId" in i}))
        for item in items:
            item["vod"] = vods.get(item.get("vodId"))


def build_services(settings: Settings) -> dict[str, Service]:
    return {
        "vods": VodsService(settings),
        "games": GamesService(settings),
        "emotes": Service(EMOTES, settings, shown=of_shown_vod(EMOTES.table.c.vod_id)),
        "streams": Service(STREAMS, settings),
    }
