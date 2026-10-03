"""Read-only Feathers-compatible services: /vods, /games, /emotes, /streams."""

from __future__ import annotations

import datetime as dt
import operator
from typing import Any
from urllib.parse import unquote_plus

from archive_common.config import Settings
from archive_common.serialize import (
    EMOTES,
    GAMES,
    STREAMS,
    VODS,
    Resource,
    attach_games,
    attach_segments,
    live_spans,
    not_hidden,
    not_merged_away,
    not_superseded,
    of_shown_vod,
    tagged,
    vods_json,
)
from sqlalchemy import ColumnElement, and_, false, func, or_, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from . import feathers_query as fq
from .errors import FeathersError, bad_literal


async def _vods_by_id(conn: AsyncConnection, vod_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not vod_ids:
        return {}
    return {v["id"]: v for v in await vods_json(conn, VODS.table.c.id.in_(vod_ids), not_hidden())}


class Service:
    def __init__(
        self,
        resource: Resource,
        settings: Settings,
        special: dict[str, fq.Special] | None = None,
        shown: ColumnElement[bool] | None = None,
    ):
        self.resource = resource
        self.settings = settings
        self.special = special or {}
        self.shown = true() if shown is None else shown  # rows that exist as far as find and get go

    async def embed(self, conn: AsyncConnection, items: list[dict[str, Any]]) -> None:
        """Associations the legacy include() hooks added."""

    def scope(self, query: dict[str, Any]) -> ColumnElement[bool]:
        """Rows ``find`` considers at all, before the query's own filters."""
        return true()

    async def specials(self, conn: AsyncConnection, qs: str) -> dict[str, fq.Special]:
        """The filters of this service's own for this query (some need to read first)."""
        return self.special

    async def find(self, conn: AsyncConnection, qs: str) -> dict[str, Any]:
        q = fq.parse(
            self.resource,
            qs,
            default_limit=self.settings.paginate_default,
            max_limit=self.settings.paginate_max,
            special=await self.specials(conn, qs),
        )
        where = and_(self.shown, self.scope(q.query), q.where)
        total = (await conn.execute(select(func.count()).select_from(self.resource.table).where(where))).scalar_one()
        data: list[dict[str, Any]] = []
        if q.limit > 0:
            stmt = (
                select(*self.resource.columns(q.select))
                .where(where)
                .order_by(*q.order_by)
                .limit(q.limit)
                .offset(q.skip)
            )
            rows = await conn.execute(stmt)
            data = [self.resource.to_json(r, q.select) for r in rows.mappings()]  # type: ignore[arg-type]
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
        item = self.resource.to_json(row)  # type: ignore[arg-type]
        await self.embed(conn, [item])
        return item


class VodsService(Service):
    def __init__(self, settings: Settings) -> None:
        special = {"chapters": fq.chapter_filter(VODS.table.c.chapters), "tags": _tags_filter}
        super().__init__(VODS, settings, special, not_hidden())

    async def embed(self, conn: AsyncConnection, items: list[dict[str, Any]]) -> None:
        await attach_games(conn, items)
        await attach_segments(conn, items)

    async def specials(self, conn: AsyncConnection, qs: str) -> dict[str, fq.Special]:
        decoded = unquote_plus(qs)
        if not any(field in decoded for field in LIVE_FIELDS):
            return self.special
        spans = await live_spans(conn)
        return {**self.special, **{field: _live_filter(field, end, spans) for field, end in LIVE_FIELDS.items()}}

    def scope(self, query: dict[str, Any]) -> ColumnElement[bool]:
        """Left out unless asked for (``GET /vods/{id}`` still answers for them all):

        * VODs merged into another one (``$merged=true``; they have ``merged_into``);
        * VODs a merge or split synthetic VOD is made of (``$superseded=true``; ``superseded_by``);
        * tagged VODs: only untagged ones are listed, unless ``$tag=<tag>`` (that tag's, e.g.
          ``compilation``), ``$tag=*`` (all) or a ``tags`` filter.

        Hidden VODs are never shown."""
        clauses = []
        if query.get("$merged") != "true":
            clauses.append(not_merged_away())
        if query.get("$superseded") != "true":
            clauses.append(not_superseded())
        tag = query.get("$tag")
        if tag is None and "tags" not in query:
            clauses.append(tagged(None))
        elif isinstance(tag, str) and tag != "*":
            clauses.append(tagged(tag))
        return and_(true(), *clauses)


# When a VOD's footage was live: a synthetic VOD's synthetic.firstLiveAt / lastLiveAt (worked out from its
# sources, as the response shows them), a real VOD's createdAt for both. The value is which end of the span.
LIVE_FIELDS = {"firstLiveAt": 0, "lastLiveAt": 1}
_LIVE_OPS = {"$lt": operator.lt, "$lte": operator.le, "$gt": operator.gt, "$gte": operator.ge}


def _when(field: str, op: str, value: Any) -> dt.datetime:
    try:
        when = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None:
        raise FeathersError(400, f"Invalid value for '{field}[{op}]'")
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _live_filter(field: str, end: int, spans: dict[str, tuple[dt.datetime, dt.datetime] | None]) -> fq.Special:
    """``firstLiveAt[$gte]=…&firstLiveAt[$lt]=…`` (``$lt $lte $gt $gte``). The synthetic VODs that match are
    picked here, by id, so totals and paging stay right; one with no live span matches nothing."""

    def build(value: Any) -> ColumnElement[bool]:
        if not isinstance(value, dict) or not value or not set(value) <= set(_LIVE_OPS):
            raise FeathersError(400, f"Invalid query parameter '{field}'")
        bounds = [(_LIVE_OPS[op], _when(field, op, v)) for op, v in value.items()]
        ids = [vod_id for vod_id, span in spans.items() if span and all(cmp(span[end], when) for cmp, when in bounds)]
        created = VODS.table.c.createdAt
        real = and_(VODS.table.c.synthetic.is_(None), *(cmp(created, when) for cmp, when in bounds))
        return or_(real, VODS.table.c.id.in_(ids))

    return build


def _tags_filter(value: Any) -> ColumnElement[bool]:
    """``tags=a`` VODs tagged ``a``; ``tags[]=a&tags[]=b`` (or ``tags[$all]``) both; ``tags[$in]`` either."""
    if isinstance(value, dict) and set(value) == {"$in"}:
        return or_(false(), *(tagged(str(t)) for t in fq._as_list(value["$in"])))
    values = fq._as_list(value["$all"]) if isinstance(value, dict) and set(value) == {"$all"} else fq._as_list(value)
    if not values or not all(isinstance(t, str) for t in values):
        raise FeathersError(400, "Invalid value for 'tags'")
    return VODS.table.c.tags.contains(values)


class GamesService(Service):
    def __init__(self, settings: Settings) -> None:
        super().__init__(GAMES, settings, shown=of_shown_vod(GAMES.table.c.vod_id))

    async def embed(self, conn: AsyncConnection, items: list[dict[str, Any]]) -> None:
        vods = await _vods_by_id(conn, list({i["vodId"] for i in items if "vodId" in i}))
        for item in items:
            item["vod"] = vods.get(item.get("vodId"))  # type: ignore[arg-type]


def build_services(settings: Settings) -> dict[str, Service]:
    return {
        "vods": VodsService(settings),
        "games": GamesService(settings),
        "emotes": Service(EMOTES, settings, shown=of_shown_vod(EMOTES.table.c.vod_id)),
        "streams": Service(STREAMS, settings),
    }
