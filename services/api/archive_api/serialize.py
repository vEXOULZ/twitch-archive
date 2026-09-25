"""Row -> JSON exactly as the legacy Feathers/Sequelize API rendered it.

* timestamps: JavaScript ``Date.toISOString()`` (UTC, milliseconds, ``Z``)
* BIGINT and NUMERIC: strings (node-postgres does not parse them)
* JSONB: passed through untouched
* attribute names: Sequelize's (``vodId`` for ``vod_id`` on games/emotes)

Fields the legacy API did not have are only ever added next to the legacy
ones (see ``vod_additions``), never replacing them.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Table

from archive_common.models import Emote, Game, Log, Stream, Vod
from archive_common.timeutil import hhmmss_to_seconds


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
    table: Table
    fields: tuple[Field, ...]
    id_key: str
    # extra accepted query names -> JSON key (raw column names Sequelize also accepted)
    aliases: Mapping[str, str]
    # adds derived fields to a rendered item, in place
    additions: Callable[[dict[str, Any]], None] | None = None

    @cached_property
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
        if self.additions is not None:
            self.additions(out)
        return out

    def columns(self, select: set[str] | None = None) -> list:
        return [f.column.label(f.key) for f in self.fields if select is None or f.key in select]


# ── Additive fields ───────────────────────────────────────────────────────

_BOX_ART_SIZE_RE = re.compile(r"-\d+x\d+(\.\w+)$")


def box_art_template(url: str | None) -> str | None:
    """Stored box art (``...-40x53.jpg``) -> the Helix ``box_art_url`` form (``...-{width}x{height}.jpg``)."""
    if not url:
        return None
    return _BOX_ART_SIZE_RE.sub(r"-{width}x{height}\1", url)


def duration_seconds(value: str | None) -> int | None:
    try:
        return hhmmss_to_seconds(value) if value else None
    except ValueError:
        return None


def chapter_additions(chapter: Any) -> Any:
    """``imageTemplate`` next to ``image``, and ``length`` next to ``end`` (which holds the length)."""
    if not isinstance(chapter, dict):
        return chapter
    out = dict(chapter)
    out.setdefault("imageTemplate", box_art_template(chapter.get("image")))
    out.setdefault("length", chapter.get("end"))
    return out


def vod_additions(vod: dict[str, Any]) -> None:
    if isinstance(vod.get("chapters"), list):
        vod["chapters"] = [chapter_additions(c) for c in vod["chapters"]]
    if "duration" in vod:
        vod["duration_seconds"] = duration_seconds(vod["duration"])


def _t(model) -> Table:
    return model.__table__


_vt, _gt, _et, _lt, _st = _t(Vod), _t(Game), _t(Emote), _t(Log), _t(Stream)

VODS = Resource(
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
    vod_additions,
)

GAMES = Resource(
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
    _et,
    (
        Field("vodId", _et.c.vod_id),
        Field("ffz_emotes", _et.c.ffz_emotes),
        Field("bttv_emotes", _et.c.bttv_emotes),
        Field("7tv_emotes", _et.c["7tv_emotes"]),
        # Added after the legacy API (not in the golden responses).
        Field("global_emotes", _et.c.global_emotes),
        Field("global_emotes_source", _et.c.global_emotes_source),
        Field("global_emotes_at", _et.c.global_emotes_at, js_iso),
        Field("createdAt", _et.c.createdAt, js_iso),
        Field("updatedAt", _et.c.updatedAt, js_iso),
    ),
    "vodId",
    {"vod_id": "vodId"},
)

LOGS = Resource(
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
