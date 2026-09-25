"""Read-only Feathers-compatible services: /vods, /games, /emotes, /streams."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from archive_common.config import Settings

from . import feathers_query as fq
from .errors import FeathersError
from .serialize import EMOTES, GAMES, STREAMS, VODS, Resource


async def _games_for(conn: AsyncConnection, vod_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {v: [] for v in vod_ids}
    if not vod_ids:
        return out
    rows = await conn.execute(
        select(*GAMES.columns()).where(GAMES.table.c.vod_id.in_(vod_ids)).order_by(GAMES.table.c.id)
    )
    for row in rows.mappings():
        out[row["vodId"]].append(GAMES.to_json(row))
    return out


async def attach_games(conn: AsyncConnection, vods: list[dict]) -> None:
    games = await _games_for(conn, [v["id"] for v in vods])
    for vod in vods:
        vod["games"] = games[vod["id"]]


async def _vods_by_id(conn: AsyncConnection, vod_ids: list[str]) -> dict[str, dict]:
    if not vod_ids:
        return {}
    rows = await conn.execute(select(*VODS.columns()).where(VODS.table.c.id.in_(vod_ids)))
    vods = {r["id"]: VODS.to_json(r) for r in rows.mappings()}
    await attach_games(conn, list(vods.values()))
    return vods


class Service:
    def __init__(self, resource: Resource, settings: Settings, special: dict[str, fq.Special] | None = None):
        self.resource = resource
        self.settings = settings
        self.special = special or {}

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        """Associations the legacy include() hooks added."""

    async def find(self, conn: AsyncConnection, qs: str) -> dict[str, Any]:
        q = fq.parse(
            self.resource,
            qs,
            default_limit=self.settings.paginate_default,
            max_limit=self.settings.paginate_max,
            special=self.special,
        )
        total = (await conn.execute(select(func.count()).select_from(self.resource.table).where(q.where))).scalar_one()
        data: list[dict] = []
        if q.limit > 0:
            stmt = (
                select(*self.resource.columns(q.select))
                .where(q.where)
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
        try:
            value = fq._value(col, id_)
            row = (await conn.execute(select(*self.resource.columns()).where(col == value))).mappings().first()
        except Exception:  # invalid literal for the id type, e.g. /streams/abc
            row = None
        if row is None:
            raise FeathersError(404, f"No record found for id '{id_}'")
        item = self.resource.to_json(row)
        await self.embed(conn, [item])
        return item


class VodsService(Service):
    def __init__(self, settings: Settings) -> None:
        super().__init__(VODS, settings, {"chapters": fq.chapter_filter(VODS.table.c.chapters)})

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        await attach_games(conn, items)


class GamesService(Service):
    def __init__(self, settings: Settings) -> None:
        super().__init__(GAMES, settings)

    async def embed(self, conn: AsyncConnection, items: list[dict]) -> None:
        vods = await _vods_by_id(conn, list({i["vodId"] for i in items if "vodId" in i}))
        for item in items:
            item["vod"] = vods.get(item.get("vodId"))


def build_services(settings: Settings) -> dict[str, Service]:
    return {
        "vods": VodsService(settings),
        "games": GamesService(settings),
        "emotes": Service(EMOTES, settings),
        "streams": Service(STREAMS, settings),
    }
