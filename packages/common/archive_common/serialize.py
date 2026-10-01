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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Table, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from archive_common.models import BotLog, Emote, Game, Log, Stream, Vod, VodSegment
from archive_common.segments import EPS, MAX_DEPTH, Segment, flatten, resolve, seconds
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


def box_art_image(template: str | None) -> str | None:
    """Helix ``box_art_url`` template -> the 40x53 image stored in chapters (inverse of ``box_art_template``)."""
    if not template:
        return None
    return template.replace("{width}", "40").replace("{height}", "53")


def duration_seconds(value: str | None) -> int | None:
    try:
        return hhmmss_to_seconds(value) if value else None
    except ValueError:
        return None


def chapter_additions(chapter: Any) -> Any:
    """``imageTemplate`` next to ``image``, and ``length`` next to ``end`` (which holds the length).
    Any other stored key, such as ``kind`` ("gap" on a merge's gap chapter), passes through."""
    if not isinstance(chapter, dict):
        return chapter
    out = dict(chapter)
    out.setdefault("imageTemplate", box_art_template(chapter.get("image")))
    out.setdefault("length", chapter.get("end"))
    return out


def vod_additions(vod: dict[str, Any]) -> None:
    if "merged_into" in vod and vod["merged_into"] is None:
        del vod["merged_into"]  # only on VODs merged into another: {"id", "offset"}
    if "synthetic" in vod and vod["synthetic"] is None:
        del vod["synthetic"]  # only on synthetic VODs; attach_segments completes it
    if isinstance(vod.get("chapters"), list):
        vod["chapters"] = [chapter_additions(c) for c in vod["chapters"]]
    if "duration" in vod:
        vod["duration_seconds"] = duration_seconds(vod["duration"])


def _t(model) -> Table:
    return model.__table__


_vt, _gt, _et, _lt, _st, _bt = _t(Vod), _t(Game), _t(Emote), _t(Log), _t(Stream), _t(BotLog)
_sgt = _t(VodSegment)

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
        # Added after the legacy API; left out of the JSON while NULL (see vod_additions).
        Field("merged_into", _vt.c.merged_into),
        Field("tags", _vt.c.tags),
        Field("synthetic", _vt.c.synthetic),
    ),
    "id",
    {},
    vod_additions,
)


def not_merged_away() -> Any:
    """VODs that were not merged into another one (lists and search leave those out)."""
    return _vt.c.merged_into.is_(None)


def not_hidden() -> Any:
    """VODs the public API shows at all: a hidden one answers like a missing one."""
    return _vt.c.hidden.is_(False)


def not_superseded() -> Any:
    """VODs no superseding synthetic VOD (a merge, a split) is made of: lists leave those out, and
    ``GET /vods/{id}`` answers for them with ``superseded_by``."""
    syn = _vt.alias("syn")
    return ~exists().where(_sgt.c.source_id == _vt.c.id, syn.c.id == _sgt.c.vod_id,
                           syn.c.synthetic["supersedes"].as_boolean().is_(True))


def tagged(tag: str | None) -> Any:
    """VODs with ``tag``; None: those with no tags at all (the regular ones)."""
    return func.cardinality(_vt.c.tags) == 0 if tag is None else _vt.c.tags.contains([tag])


def real() -> Any:
    """Real VODs (not synthetic ones)."""
    return _vt.c.synthetic.is_(None)


def of_shown_vod(vod_id: Any) -> Any:
    """Rows (games, emotes) whose ``vod_id`` is not a hidden VOD's."""
    return ~exists().where(_vt.c.id == vod_id, _vt.c.hidden.is_(True))


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
    lambda row: row.update(source="replay"),  # the Twitch VOD replay (the fallback)
)

# doomtp-bot's chat: the LOGS fields (createdAt is when it was sent, like the replay's),
# then what only the bot has; ``bot`` is its entry as fetched.
BOT_LOGS = Resource(
    _bt,
    (
        Field("id", _bt.c.id),
        Field("_id", _bt.c.seq),
        Field("vod_id", _bt.c.vod_id),
        Field("display_name", _bt.c.display_name),
        Field("content_offset_seconds", _bt.c.content_offset_seconds),
        Field("message", _bt.c.message),
        Field("user_badges", _bt.c.user_badges),
        Field("user_color", _bt.c.user_color),
        Field("createdAt", _bt.c.at, js_iso),
        Field("updatedAt", _bt.c.updatedAt, js_iso),
        Field("kind", _bt.c.kind),
        Field("user_id", _bt.c.user_id),
        Field("user_login", _bt.c.user_login),
        Field("message_type", _bt.c.message_type),
        Field("deleted_at", _bt.c.deleted_at, js_iso),
        Field("cleared_at", _bt.c.cleared_at, js_iso),
        Field("bot", _bt.c.data),
    ),
    "id",
    {},
    lambda row: row.update(source="bot"),
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


# ── Associations ──────────────────────────────────────────────────────────


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
    """``games`` on each VOD, as the legacy include() hook added it."""
    games = await _games_for(conn, [v["id"] for v in vods])
    for vod in vods:
        vod["games"] = games[vod["id"]]


def _game_in(game: dict, seg: Segment, synthetic_id: str) -> dict | None:
    """A source's game row cut to ``seg``'s window and moved to where it plays on the synthetic VOD."""
    try:
        start, end = float(game["start_time"]), float(game["end_time"])
    except (TypeError, ValueError):
        return None
    start, end = max(start, seg.start), min(end, seg.end)
    if end - start <= EPS:
        return None
    shift = seg.at - seg.start
    return {**game, "vodId": synthetic_id, "sourceVodId": game["vodId"],
            "start_time": str(seconds(start + shift)), "end_time": str(seconds(end + shift))}


async def nested_segments(conn: AsyncConnection | AsyncSession, ids: Iterable[str],
                          levels: int) -> tuple[dict[str, list[Segment]], dict[str, bool]]:
    """The stored segments of each synthetic VOD among ``ids``, then of the synthetic VODs they are
    made of, ``levels`` levels down; and whether each of those supersedes its sources."""
    raw: dict[str, list[Segment]] = {}
    supersedes: dict[str, bool] = {}
    todo = set(ids)
    for _ in range(levels):
        todo -= raw.keys()
        if not todo:
            break
        rows = (await conn.execute(
            select(_sgt, _vt.c.synthetic).join(_vt, _vt.c.id == _sgt.c.vod_id)
            .where(_sgt.c.vod_id.in_(todo)).order_by(_sgt.c.vod_id, _sgt.c.pos)
        )).mappings().all()
        for r in rows:
            raw.setdefault(r["vod_id"], []).append(Segment.of_row(r))
            supersedes[r["vod_id"]] = bool((r["synthetic"] or {}).get("supersedes"))
        todo = {x.source_id for k in todo for x in raw.get(k, [])}
    return raw, supersedes


async def flat_segments(conn: AsyncConnection, ids: list[str]) -> dict[str, list[Segment]]:
    """The segments of each synthetic VOD in ``ids``, resolved and flattened (``segments.flatten``):
    windows of real VODs only, each with its stream. A VOD that isn't synthetic has no entry."""
    raw, supersedes = await nested_segments(conn, ids, MAX_DEPTH + 2)  # each level's sources, then theirs
    if not raw:
        return {}
    sources = {x.source_id for v in raw.values() for x in v}
    durations = {vod_id: float(duration_seconds(duration) or 0)
                 for vod_id, duration in await conn.execute(select(_vt.c.id, _vt.c.duration).where(_vt.c.id.in_(sources)))}
    resolved = {k: resolve(v, durations) for k, v in raw.items()}
    return {k: flatten(resolved[k], resolved, supersedes, supersedes[k]) for k in ids if k in resolved}


def _live_span(segs: list[Segment], live: Mapping[str, dt.datetime | None]) -> dict[str, str | None]:
    """When the earliest and the latest of ``segs``'s footage was live (a source's start plus the
    seconds into it, as ``createdAt`` counts them)."""
    at = [(live[x.source_id] + dt.timedelta(seconds=x.start), live[x.source_id] + dt.timedelta(seconds=x.end or x.start))
          for x in segs if live.get(x.source_id)]
    return {"firstLiveAt": js_iso(min(a for a, _ in at)) if at else None,
            "lastLiveAt": js_iso(max(b for _, b in at)) if at else None}


async def attach_segments(conn: AsyncConnection, vods: list[dict]) -> None:
    """Synthetic VODs get ``synthetic.segments`` (``flat_segments``: real VODs only, ends resolved,
    streams numbered), ``madeAt`` and ``changedAt`` (as stored), ``firstLiveAt`` and ``lastLiveAt`` (``_live_span``) and the games of their sources' windows (after ``attach_games``); a VOD a shown synthetic VOD is made of gets
    ``superseded_by`` (merges, splits: where each part of it went) or ``appears_in`` (the others)."""
    ids = [v["id"] for v in vods if "id" in v]
    if not ids:
        return
    syn = _vt.alias("syn")
    rows = (await conn.execute(
        select(_sgt, syn.c.title, syn.c.tags, syn.c.synthetic, syn.c.hidden)
        .join(syn, syn.c.id == _sgt.c.vod_id)
        .where(or_(_sgt.c.vod_id.in_(ids), _sgt.c.source_id.in_(ids)))
        .order_by(syn.c.createdAt, _sgt.c.vod_id, _sgt.c.pos)
    )).mappings().all()
    if not rows:
        return
    resolved = await flat_segments(conn, list({r["vod_id"] for r in rows if r["vod_id"] in ids}))
    real = list({x.source_id for v in resolved.values() for x in v})
    games = await _games_for(conn, real)
    live = dict((await conn.execute(select(_vt.c.id, _vt.c.createdAt).where(_vt.c.id.in_(real)))).all()) if real else {}
    # Where the parts of each source went, for superseded_by / appears_in.
    used: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["source_id"] not in ids or r["hidden"]:
            continue
        entry = used.setdefault(r["source_id"], {})
        if (r["synthetic"] or {}).get("supersedes"):
            seg = Segment.of_row(r)
            entry.setdefault("superseded_by", []).append(
                {"id": r["vod_id"], "start": seconds(seg.start), "end": None if seg.end is None else seconds(seg.end),
                 "at": seconds(seg.at)})
        else:
            entry.setdefault("appears_in", {}).setdefault(
                r["vod_id"], {"id": r["vod_id"], "title": r["title"], "tags": list(r["tags"] or [])})
    for vod in vods:
        segs = resolved.get(vod["id"])
        if segs is not None and "synthetic" in vod:  # (not when $select left it out)
            meta = vod["synthetic"] or {}
            vod["synthetic"] = {"supersedes": bool(meta.get("supersedes")), "segments": [x.json() for x in segs],
                                "madeAt": meta.get("madeAt"), "changedAt": meta.get("changedAt"),
                                **_live_span(segs, live)}
            if "games" in vod:
                vod["games"] = [g for x in segs for g in (_game_in(g, x, vod["id"]) for g in games.get(x.source_id, []))
                                if g is not None]
        entry = used.get(vod["id"])
        if entry:
            if "superseded_by" in entry:
                vod["superseded_by"] = entry["superseded_by"]
            if "appears_in" in entry:
                vod["appears_in"] = list(entry["appears_in"].values())


async def vods_json(
    conn: AsyncConnection, *where: Any, order_by: Any = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """VODs matching ``where``, each exactly as ``GET /vods/{id}`` renders it."""
    stmt = select(*VODS.columns()).where(*where).limit(limit)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    vods = [VODS.to_json(r) for r in (await conn.execute(stmt)).mappings()]
    await attach_games(conn, vods)
    await attach_segments(conn, vods)
    return vods


async def vod_json(conn: AsyncConnection, vod_id: str) -> dict[str, Any] | None:
    """One VOD exactly as ``GET /vods/{id}`` renders it, or None."""
    vods = await vods_json(conn, VODS.table.c.id == vod_id)
    return vods[0] if vods else None
