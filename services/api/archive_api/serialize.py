"""Row -> JSON exactly as the legacy Feathers/Sequelize API rendered it.

* timestamps: JavaScript ``Date.toISOString()`` (UTC, milliseconds, ``Z``)
* BIGINT and NUMERIC: strings (node-postgres does not parse them)
* JSONB: passed through untouched
* attribute names: Sequelize's (``vodId`` for ``vod_id`` on games/emotes)
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Table

from archive_common.models import Emote, Game, Log, Stream, Vod


def js_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    value = value.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        # node-postgres returns the text representation Postgres sent
        return format(value, "f")
    return str(value)


def _plain(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class Field:
    key: str  # JSON / Feathers attribute name
    column: Column
    convert: Callable[[Any], Any] = _plain


@dataclass(frozen=True)
class Resource:
    name: str
    table: Table
    fields: tuple[Field, ...]
    id_key: str
    # extra accepted query names -> JSON key (raw column names Sequelize also accepted)
    aliases: Mapping[str, str]

    @property
    def by_key(self) -> dict[str, Field]:
        return {f.key: f for f in self.fields}

    def field(self, name: str) -> Field | None:
        name = self.aliases.get(name, name)
        return self.by_key.get(name)

    def to_json(self, row: Mapping[str, Any], select: set[str] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.fields:
            if select is not None and f.key not in select:
                continue
            out[f.key] = f.convert(row[f.key])
        return out

    def columns(self, select: set[str] | None = None) -> list:
        return [f.column.label(f.key) for f in self.fields if select is None or f.key in select]


def _t(model) -> Table:
    return model.__table__


_vt, _gt, _et, _lt, _st = _t(Vod), _t(Game), _t(Emote), _t(Log), _t(Stream)

VODS = Resource(
    "vods",
    _vt,
    (
        Field("id", _vt.c.id),
        Field("chapters", _vt.c.chapters),
        Field("title", _vt.c.title),
        Field("duration", _vt.c.duration),
        Field("thumbnail_url", _vt.c.thumbnail_url),
        Field("youtube", _vt.c.youtube),
        Field("stream_id", _vt.c.stream_id),
        Field("drive", _vt.c.drive),
        Field("platform", _vt.c.platform),
        Field("createdAt", _vt.c.createdAt, js_iso),
        Field("updatedAt", _vt.c.updatedAt, js_iso),
    ),
    "id",
    {},
)

GAMES = Resource(
    "games",
    _gt,
    (
        Field("id", _gt.c.id, _as_str),
        Field("vodId", _gt.c.vod_id),
        Field("start_time", _gt.c.start_time, _as_str),
        Field("end_time", _gt.c.end_time, _as_str),
        Field("video_provider", _gt.c.video_provider),
        Field("video_id", _gt.c.video_id),
        Field("thumbnail_url", _gt.c.thumbnail_url),
        Field("game_id", _gt.c.game_id),
        Field("game_name", _gt.c.game_name),
        Field("title", _gt.c.title),
        Field("chapter_image", _gt.c.chapter_image),
        Field("createdAt", _gt.c.createdAt, js_iso),
        Field("updatedAt", _gt.c.updatedAt, js_iso),
    ),
    "id",
    {"vod_id": "vodId"},
)

EMOTES = Resource(
    "emotes",
    _et,
    (
        Field("vodId", _et.c.vod_id),
        Field("ffz_emotes", _et.c.ffz_emotes),
        Field("bttv_emotes", _et.c.bttv_emotes),
        Field("7tv_emotes", _et.c["7tv_emotes"]),
        Field("createdAt", _et.c.createdAt, js_iso),
        Field("updatedAt", _et.c.updatedAt, js_iso),
    ),
    "vodId",
    {"vod_id": "vodId"},
)

LOGS = Resource(
    "logs",
    _lt,
    (
        Field("id", _lt.c.id, lambda v: str(v) if isinstance(v, uuid.UUID) else v),
        Field("_id", _lt.c["_id"]),
        Field("vod_id", _lt.c.vod_id),
        Field("display_name", _lt.c.display_name),
        Field("content_offset_seconds", _lt.c.content_offset_seconds),
        Field("message", _lt.c.message),
        Field("user_badges", _lt.c.user_badges),
        Field("user_color", _lt.c.user_color),
        Field("createdAt", _lt.c.createdAt, js_iso),
        Field("updatedAt", _lt.c.updatedAt, js_iso),
    ),
    "id",
    {},
)

STREAMS = Resource(
    "streams",
    _st,
    (
        Field("id", _st.c.id, _as_str),
        Field("started_at", _st.c.started_at, js_iso),
        Field("platform", _st.c.platform),
        Field("is_live", _st.c.is_live),
    ),
    "id",
    {},
)
