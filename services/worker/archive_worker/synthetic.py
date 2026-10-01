"""Synthetic VODs in the database: create, change, delete and recompose them (admin API, monitor).

A synthetic VOD is a ``vods`` row with ``synthetic`` set and its ``vod_segments`` (see ``compose``).
Its sources are only read, never written: deleting it is the whole undo. Each change is one
transaction with the synthetic row locked. Writes to ``vods`` NOTIFY archive-api (migration 0006);
``vod_segments`` has no trigger, so a change NOTIFYs each source too, whose JSON lists the
synthetic VODs it is in (``superseded_by``, ``appears_in``).

``recompose`` writes a synthetic VOD's cached columns from its sources again. The monitor runs
``recompose_stale`` every round, which picks the ones with a source updated since: that is how a
backfill or an edit of a source reaches the synthetic VODs made of it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from archive_common.db import VOD_CHANGED, get_sessionmaker
from archive_common.models import Vod, VodSegment
from archive_common.segments import EPS, Segment, resolve
from archive_common.timeutil import hhmmss_to_seconds

from . import compose
from .compose import ComposeError, Source
from .events import iso_utc
from .vod_edits import tags as check_tags

log = logging.getLogger(__name__)
STALE_BATCH = 50


class SyntheticError(Exception):
    def __init__(self, status: int, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra


def source_of(vod: Vod) -> Source:
    return Source(vod.id, float(hhmmss_to_seconds(vod.duration)), list(vod.chapters or []), vod.created_at,
                  vod.thumbnail_url, vod.title, vod.synthetic is not None)


def _composed(fn, *args):
    try:
        return fn(*args)
    except ComposeError as exc:
        raise SyntheticError(422, str(exc), **exc.extra) from exc
    except ValueError as exc:
        raise SyntheticError(422, str(exc)) from exc


async def _segments(s: AsyncSession, vod_id: str) -> list[Segment]:
    rows = (await s.execute(select(VodSegment).where(VodSegment.vod_id == vod_id).order_by(VodSegment.pos))).scalars()
    return [Segment.of_row(r) for r in rows]


async def _sources(s: AsyncSession, ids: set[str]) -> dict[str, Source]:
    return {v.id: source_of(v) for v in (await s.execute(select(Vod).where(Vod.id.in_(ids)))).scalars()}


async def _locked(s: AsyncSession, vod_id: str) -> Vod:
    vod = (await s.execute(select(Vod).where(Vod.id == vod_id).with_for_update())).scalar_one_or_none()
    if vod is None:
        raise SyntheticError(404, f"no VOD {vod_id}")
    if vod.synthetic is None:
        raise SyntheticError(409, f"{vod_id} is a real VOD, not a synthetic one")
    return vod


async def _notify(s: AsyncSession, *vod_ids: str) -> None:
    for vod_id in sorted(set(vod_ids)):
        await s.execute(select(func.pg_notify(VOD_CHANGED, vod_id)))


async def _no_double_cover(s: AsyncSession, vod_id: str, segments: list[Segment], sources: dict[str, Source]) -> None:
    """A source's second may be superseded by one synthetic VOD only, or its URL could not redirect."""
    durations = {k: v.duration for k, v in sources.items()}
    mine = resolve(segments, durations)
    others = (await s.execute(
        select(VodSegment, Vod.synthetic).join(Vod, Vod.id == VodSegment.vod_id)
        .where(VodSegment.source_id.in_({x.source_id for x in mine}), VodSegment.vod_id != vod_id)
    )).all()
    by_synthetic: dict[str, list[Segment]] = {}
    for row, synthetic in others:
        if (synthetic or {}).get("supersedes"):
            by_synthetic.setdefault(row.vod_id, []).append(Segment.of_row(row))
    for other_id, theirs in by_synthetic.items():
        theirs.sort(key=lambda x: x.at)
        for a in mine:
            for b in resolve(theirs, durations):
                if a.source_id == b.source_id and min(a.end, b.end) - max(a.start, b.start) > EPS:
                    raise SyntheticError(409, f"{a.source_id} {max(a.start, b.start):g}-{min(a.end, b.end):g}s is "
                                              f"already superseded by {other_id}; delete or change that one first",
                                         synthetic=other_id, vodId=a.source_id)


async def _write_segments(s: AsyncSession, vod_id: str, segments: list[Segment]) -> None:
    await s.execute(delete(VodSegment).where(VodSegment.vod_id == vod_id))
    if segments:
        await s.execute(insert(VodSegment).values(
            [{"vod_id": vod_id, "pos": i, **seg.columns()} for i, seg in enumerate(segments)]))


def _apply(vod: Vod, derived: dict[str, Any]) -> None:
    vod.duration, vod.chapters = derived["duration"], derived["chapters"]
    vod.thumbnail_url = derived["thumbnail_url"]
    if derived["created_at"] is not None:
        vod.created_at = derived["created_at"]


def synthetic_json(vod: Vod, segments: list[Segment]) -> dict[str, Any]:
    return {"id": vod.id, "title": vod.title, "supersedes": bool((vod.synthetic or {}).get("supersedes")),
            "tags": list(vod.tags or []), "hidden": vod.hidden, "duration": vod.duration,
            "createdAt": iso_utc(vod.created_at), "segments": [seg.json() for seg in segments]}


async def _checked(s: AsyncSession, vod_id: str, segments: list[Segment], supersedes: bool) -> dict[str, Source]:
    sources = await _sources(s, {seg.source_id for seg in segments})
    _composed(compose.validate, segments, sources)
    if supersedes:
        await _no_double_cover(s, vod_id, segments, sources)
    return sources


# ── Changes ───────────────────────────────────────────────────────────────


async def create(vod_id: str, segments: list[Segment], *, title: str | None = None, supersedes: bool = False,
                 tags: Any = (), hidden: bool = False) -> dict[str, Any]:
    """A new synthetic VOD; ``title`` defaults to the first source's."""
    _composed(compose.check_id, vod_id)
    tags = _composed(check_tags, list(tags))
    async with get_sessionmaker()() as s, s.begin():
        if await s.get(Vod, vod_id) is not None:
            raise SyntheticError(409, f"There is already a VOD {vod_id}", vodId=vod_id)
        sources = await _checked(s, vod_id, segments, supersedes)
        first = sources[segments[0].source_id]
        vod = Vod(id=vod_id, title=(title or "").strip() or first.title, platform="twitch", chapters_locked=True,
                  synthetic={"supersedes": supersedes}, tags=tags, hidden=hidden, youtube=[], drive=[])
        _apply(vod, compose.derive(segments, sources))
        s.add(vod)
        await s.flush()
        await _write_segments(s, vod_id, segments)
        await _notify(s, *sources)
        return synthetic_json(vod, segments)


async def change(vod_id: str, *, segments: list[Segment] | None = None, title: str | None = None,
                 supersedes: bool | None = None, tags: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change what is given; returns the synthetic VOD (as ``synthetic_json``) before and after."""
    if tags is not None:
        tags = _composed(check_tags, tags)
    async with get_sessionmaker()() as s, s.begin():
        vod = await _locked(s, vod_id)
        old = await _segments(s, vod_id)
        before = synthetic_json(vod, old)
        new = old if segments is None else segments
        supersedes = before["supersedes"] if supersedes is None else supersedes
        sources = await _checked(s, vod_id, new, supersedes)
        if title is not None:
            if not title.strip():
                raise SyntheticError(422, "title must be a non-empty string")
            vod.title = title.strip()
        if tags is not None:
            vod.tags = tags
        vod.synthetic = {**(vod.synthetic or {}), "supersedes": supersedes}
        _apply(vod, compose.derive(new, sources))
        if segments is not None:
            await _write_segments(s, vod_id, new)
        await _notify(s, *{x.source_id for x in old}, *sources)
        await s.flush()
        return before, synthetic_json(vod, new)


async def remove(vod_id: str) -> dict[str, Any]:
    """Delete a synthetic VOD (its segments go with it); its sources are as they were. Returns what it was."""
    async with get_sessionmaker()() as s, s.begin():
        vod = await _locked(s, vod_id)
        segments = await _segments(s, vod_id)
        before = synthetic_json(vod, segments)
        await s.execute(delete(Vod).where(Vod.id == vod_id))
        await _notify(s, *{x.source_id for x in segments})
        return before


async def get(vod_id: str) -> dict[str, Any]:
    async with get_sessionmaker()() as s:
        vod = await s.get(Vod, vod_id)
        if vod is None:
            raise SyntheticError(404, f"no VOD {vod_id}")
        if vod.synthetic is None:
            raise SyntheticError(409, f"{vod_id} is a real VOD, not a synthetic one")
        return synthetic_json(vod, await _segments(s, vod_id))


async def containing(source_id: str) -> list[dict[str, Any]]:
    """The synthetic VODs made with ``source_id``, each with the segments of it they use."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(
            select(VodSegment, Vod).join(Vod, Vod.id == VodSegment.vod_id)
            .where(VodSegment.source_id == source_id).order_by(Vod.created_at, Vod.id, VodSegment.pos)
        )).all()
    out: dict[str, dict[str, Any]] = {}
    for seg, vod in rows:
        entry = out.setdefault(vod.id, {"id": vod.id, "title": vod.title, "tags": list(vod.tags or []),
                                        "supersedes": bool((vod.synthetic or {}).get("supersedes")),
                                        "segments": []})
        entry["segments"].append(Segment.of_row(seg).json())
    return list(out.values())


# ── Freshness ─────────────────────────────────────────────────────────────


async def recompose(*vod_ids: str) -> list[str]:
    """Write these synthetic VODs' cached columns from their sources again; returns the ones that exist.
    The vods trigger tells archive-api when anything actually changed."""
    done = []
    for vod_id in vod_ids:
        async with get_sessionmaker()() as s, s.begin():
            vod = (await s.execute(select(Vod).where(Vod.id == vod_id, Vod.synthetic.is_not(None))
                                   .with_for_update(skip_locked=True))).scalar_one_or_none()
            if vod is None:
                continue
            segments = await _segments(s, vod_id)
            sources = await _sources(s, {x.source_id for x in segments})
            if segments and all(x.source_id in sources for x in segments):
                _apply(vod, compose.derive(segments, sources))
            vod.updated_at = func.now()  # done, even when nothing changed (that sends no NOTIFY)
            done.append(vod_id)
    return done


async def synthetic_ids_of(*source_ids: str) -> list[str]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(
            select(VodSegment.vod_id).where(VodSegment.source_id.in_(source_ids)).distinct().order_by(VodSegment.vod_id)
        )).scalars())


async def stale_ids(limit: int = STALE_BATCH) -> list[str]:
    """Synthetic VODs with a source updated after they were last composed."""
    src = Vod.__table__.alias("src")
    async with get_sessionmaker()() as s:
        return list((await s.execute(
            select(VodSegment.vod_id)
            .join(Vod, Vod.id == VodSegment.vod_id)
            .join(src, src.c.id == VodSegment.source_id)
            .group_by(VodSegment.vod_id, Vod.updated_at)
            .having(func.max(src.c.updatedAt) > Vod.updated_at)
            .order_by(VodSegment.vod_id).limit(limit)
        )).scalars())


async def recompose_stale() -> list[str]:
    ids = await stale_ids()
    if ids:
        done = await recompose(*ids)
        log.info("recomposed synthetic VOD(s) %s", ", ".join(done))
        return done
    return []
