"""Merging two VODs of one broadcast, splitting a VOD in two, and undoing either (admin API).

Each operation is one transaction with the VOD rows locked, and moves rows rather than
fetching anything again: chapters, uploads and drive entries (see ``timeline`` for the
numbers), ``games`` rows, emotes, and chat rows re-keyed with their offsets shifted by a
whole number of seconds, so every comment keeps its place against the video.

A ``vod_splices`` row records what it did and what the rows were before, so it can be
undone; only the latest splice touching either VOD can be (undo them in reverse order).
Writes to ``vods`` NOTIFY archive-api (migration 0006), which drops its cached copies;
a ROWS_MOVED notice for each VOD drops its cached chat and emotes too.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import delete, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from archive_common.db import ROWS_MOVED, VOD_CHANGED, get_sessionmaker
from archive_common.models import Emote, Game, Job, Log, Vod, VodSplice, VodSpliceLog
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds

from . import timeline
from .events import iso_utc
from .jobs import ACTIVE
from .timeline import Plan, PlanError, Side
from .vods import active_splices as _active_query

# Rows of a VOD that a splice replaces, and restores on undo.
FIELDS = ("title", "duration", "chapters", "youtube", "drive", "chapters_locked", "thumbnail_url")
# Fields an undo checks for edits made since (the rest only the splice writes).
EDITABLE = ("title", "duration", "chapters", "youtube", "drive")
EMOTE_COLUMNS = (*timeline.EMOTE_SETS, "global_emotes", "global_emotes_source", "global_emotes_at")


class SpliceError(Exception):
    def __init__(self, status: int, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra


def _session() -> AsyncSession:
    return get_sessionmaker()()


def _whole_seconds(value: Any, name: str) -> int:
    if not timeline.is_number(value) or value < 0 or not float(value).is_integer():
        raise SpliceError(400, f"{name} must be a whole number of seconds >= 0")
    return int(value)


def _side(vod: Vod) -> Side:
    return Side(vod.id, hhmmss_to_seconds(vod.duration), list(vod.chapters or []), list(vod.youtube or []),
                list(vod.drive or []))


def _fields(vod: Vod) -> dict[str, Any]:
    return {k: getattr(vod, k) for k in FIELDS}


def _restore(vod: Vod, fields: dict[str, Any]) -> None:
    for k in FIELDS:
        setattr(vod, k, fields[k])


def _apply(vod: Vod, plan: Plan) -> None:
    vod.duration, vod.chapters, vod.youtube, vod.drive = format_hhmmss(plan.duration), plan.chapters, plan.youtube, plan.drive
    vod.chapters_locked = True  # the automatic chapters step would put Twitch's (one-VOD) chapters back


async def _rows_moved(s: AsyncSession, *vod_ids: str) -> None:
    """Tell archive-api (on commit) that these VODs' chat rows and emotes moved."""
    for vod_id in vod_ids:
        await s.execute(select(func.pg_notify(VOD_CHANGED, ROWS_MOVED + vod_id)))


def splice_json(sp: VodSplice) -> dict[str, Any]:
    return {
        "id": sp.id,
        "kind": sp.kind,
        "vodId": sp.vod_id,
        "otherId": sp.other_id,
        "offset": timeline.num_seconds(sp.offset_s),
        "gap": sp.detail.get("gap"),  # merges only
        "detail": sp.detail,
        "createdAt": iso_utc(sp.created_at),
        "undoneAt": iso_utc(sp.undone_at),
    }


# ── Locks and checks ──────────────────────────────────────────────────────


async def _lock(s: AsyncSession, *vod_ids: str) -> list[Vod]:
    """The rows, locked for the transaction (in id order, so two splices never deadlock)."""
    rows = {v.id: v for v in (await s.execute(
        select(Vod).where(Vod.id.in_(vod_ids)).order_by(Vod.id).with_for_update()
    )).scalars()}
    for vod_id in vod_ids:
        if vod_id not in rows:
            raise SpliceError(404, f"No Vod Data for {vod_id}")
    return [rows[v] for v in vod_ids]


async def _no_active_jobs(s: AsyncSession, *vod_ids: str) -> None:
    active = (await s.execute(
        select(Job).where(Job.vod_id.in_(vod_ids), Job.state.in_(ACTIVE)).order_by(Job.id)
    )).scalars().all()
    if active:
        raise SpliceError(409, "Jobs are active on " + ", ".join(sorted(vod_ids)) + ": " + ", ".join(
            f"{j.id} ({j.kind}, {j.state}, vod {j.vod_id})" for j in active) + "; wait for them or cancel them",
            jobs=[j.id for j in active])


def _not_merged_away(*vods: Vod) -> None:
    for vod in vods:
        if vod.merged_into:
            raise SpliceError(409, f"{vod.id} is already merged into {vod.merged_into.get('id')}",
                              mergedInto=vod.merged_into)


async def _active(s: AsyncSession, *vod_ids: str) -> list[VodSplice]:
    """Splices touching any of ``vod_ids`` that are not undone, oldest first."""
    return list((await s.execute(_active_query(*vod_ids))).scalars())


def _blocker(splice: VodSplice, active: list[VodSplice]) -> VodSplice | None:
    """The latest of ``active`` made after ``splice`` on either of its VODs (to be undone first)."""
    ids = {splice.vod_id, splice.other_id}
    later = [sp for sp in active if sp.id > splice.id and ids & {sp.vod_id, sp.other_id}]
    return later[-1] if later else None


async def _blocking_splice(s: AsyncSession, splice: VodSplice) -> VodSplice | None:
    return _blocker(splice, await _active(s, splice.vod_id, splice.other_id))


async def _undoable(s: AsyncSession, splice: VodSplice) -> None:
    later = await _blocking_splice(s, splice)
    if later is not None:
        raise SpliceError(409, f"{later.vod_id} was {'merged with' if later.kind == 'merge' else 'split into'} "
                               f"{later.other_id} since (splice {later.id}); undo that first",
                          blockedBy=splice_json(later))


def _unedited(splice: VodSplice, vods: dict[str, Vod], force: bool) -> None:
    """Refuse an undo that would throw away hand edits made since the splice (unless forced)."""
    if force:
        return
    edited = [f"{vod_id}.{k}" for vod_id, fields in splice.snapshot["result"].items()
              for k in EDITABLE if getattr(vods[vod_id], k) != fields[k]]
    if edited:
        raise SpliceError(409, f"Edited since the {splice.kind}: {', '.join(edited)}. Undoing it restores the rows "
                               "as they were before, losing those edits; pass force to do it anyway",
                          edited=edited)


async def _emotes(s: AsyncSession, vod_id: str) -> Emote | None:
    return (await s.execute(select(Emote).where(Emote.vod_id == vod_id).with_for_update())).scalar_one_or_none()


def _emote_values(row: Emote | None) -> dict[str, Any] | None:
    return None if row is None else {k: getattr(row, k) for k in EMOTE_COLUMNS}


def _emote_json(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    at = values["global_emotes_at"]
    return {**values, "global_emotes_at": at.isoformat() if at else None}


async def _set_emotes(s: AsyncSession, vod_id: str, row: Emote | None, values: dict[str, Any] | None) -> None:
    """Replace ``vod_id``'s emotes ``row`` (locked, or None) with ``values`` (EMOTE_COLUMNS; None: no row)."""
    if values is None:
        if row is not None:
            await s.delete(row)
        return
    at = values.get("global_emotes_at")
    values = {**values, "global_emotes_at": dt.datetime.fromisoformat(at) if isinstance(at, str) else at}
    if row is None:
        s.add(Emote(vod_id=vod_id, **values))
    else:
        for k, v in values.items():
            setattr(row, k, v)


async def _repoint(s: AsyncSession, from_id: str, to_id: str, by: float, *, at_least: float | None = None
                   ) -> dict[str, Any]:
    """VODs merged into ``from_id`` (at an offset >= ``at_least``) now point at ``to_id``, their
    offset moved by ``by``, so the site's redirect stays one hop. Returns the old values."""
    old = {}
    for vod in (await s.execute(
        select(Vod).where(Vod.merged_into["id"].astext == from_id).with_for_update()
    )).scalars():
        offset = vod.merged_into.get("offset") or 0
        if at_least is None or offset >= at_least:
            old[vod.id] = vod.merged_into
            vod.merged_into = {"id": to_id, "offset": timeline.num_seconds(offset + by)}
    return old


async def _unpoint(s: AsyncSession, old: dict[str, Any]) -> None:
    for vod_id, merged_into in old.items():
        await s.execute(update(Vod).where(Vod.id == vod_id).values(merged_into=merged_into))


def _plan(fn, *args):
    try:
        return fn(*args)
    except PlanError as exc:
        raise SpliceError(409, str(exc), **exc.extra) from exc


# ── Merge ─────────────────────────────────────────────────────────────────


async def merge(target_id: str, source_id: str, gap: Any = None) -> dict[str, Any]:
    """Append ``source`` (the later VOD) to ``target``; ``gap`` (seconds) overrides the one
    computed from the two start times."""
    if target_id == source_id:
        raise SpliceError(400, "A VOD cannot be merged with itself")
    gap_override = None if gap is None else _whole_seconds(gap, "gap")
    async with _session() as s, s.begin():
        a, b = await _lock(s, target_id, source_id)
        _not_merged_away(a, b)
        if b.created_at < a.created_at:
            raise SpliceError(409, f"{b.id} started before {a.id}; merge the later VOD into the earlier one "
                                   f"(POST /admin/vods/{b.id}/merge with source {a.id})")
        await _no_active_jobs(s, a.id, b.id)
        side_a, side_b = _side(a), _side(b)
        computed = round((b.created_at - a.created_at).total_seconds())
        offset = computed if gap_override is None else side_a.duration + gap_override
        plan = _plan(timeline.plan_merge, side_a, side_b, offset)
        before = {"target": _fields(a), "source": _fields(b)}

        emotes_a = await _emotes(s, a.id)
        target_emotes = _emote_values(emotes_a)
        await _set_emotes(s, a.id, emotes_a,
                          timeline.union_emotes(target_emotes, _emote_values(await _emotes(s, b.id))))

        games = sorted((await s.execute(update(Game).where(Game.vod_id == b.id).values(
            vod_id=a.id, start_time=Game.start_time + offset, end_time=Game.end_time + offset,
        ).returning(Game.id))).scalars())

        splice = VodSplice(kind="merge", vod_id=a.id, other_id=b.id, offset_s=offset, detail={}, snapshot={})
        s.add(splice)
        await s.flush()
        await s.execute(insert(VodSpliceLog).from_select(
            ["splice_id", "log_id"], select(literal(splice.id), Log.id).where(Log.vod_id == b.id)))
        comments = (await s.execute(update(Log).where(Log.vod_id == b.id).values(
            vod_id=a.id, content_offset_seconds=Log.content_offset_seconds + offset))).rowcount
        repointed = await _repoint(s, b.id, a.id, offset)

        _apply(a, plan)
        b.chapters, b.youtube, b.drive = [], [], []  # they live in A now
        b.merged_into = {"id": a.id, "offset": offset}

        splice.detail = {
            **plan.detail,
            "computedOffset": computed,
            "computedGap": computed - side_a.duration,
            "gapOverridden": gap_override is not None,
            "movedComments": comments,
            "movedGames": games,
            "repointed": sorted(repointed),
        }
        splice.snapshot = {**before, "targetEmotes": _emote_json(target_emotes), "games": games,
                           "repointed": repointed, "result": {a.id: _fields(a), b.id: _fields(b)}}
        await _rows_moved(s, a.id, b.id)
        await s.flush()
        return {"splice": splice_json(splice), "warnings": _drift_warnings(plan.detail)}


def _drift_warnings(detail: dict) -> list[str]:
    """One gap chapter serves every upload type, but it can only fit the played type exactly."""
    return [
        f"The target's {typ} uploads now play {n['drift']}s off: the gap chapter fits the {detail['playedType']} "
        f"uploads, and the source's {typ} uploads start {n['drift']}s differently from those"
        for typ, n in detail["types"].items() if abs(n["drift"]) > 1
    ]


async def _undo(s: AsyncSession, splice: VodSplice, force: bool) -> dict[str, Any]:
    """Put both VODs' rows back as they were before ``splice`` (a merge or a split)."""
    a, b = await _lock(s, splice.vod_id, splice.other_id)
    await _undoable(s, splice)
    await _no_active_jobs(s, a.id, b.id)
    _unedited(splice, {a.id: a, b.id: b}, force)
    await (_unmerge_rows if splice.kind == "merge" else _unsplit_rows)(s, splice, a, b)
    await _unpoint(s, splice.snapshot["repointed"])
    _restore(a, splice.snapshot["target"])
    await _rows_moved(s, a.id, b.id)
    splice.undone_at = func.now()
    await s.flush()
    await s.refresh(splice)
    return {"splice": splice_json(splice)}


async def _unmerge_rows(s: AsyncSession, splice: VodSplice, a: Vod, b: Vod) -> None:
    offset = int(splice.offset_s)
    snap = splice.snapshot
    await s.execute(update(Log).where(Log.id == VodSpliceLog.log_id, VodSpliceLog.splice_id == splice.id).values(
        vod_id=b.id, content_offset_seconds=Log.content_offset_seconds - offset))
    await s.execute(delete(VodSpliceLog).where(VodSpliceLog.splice_id == splice.id))
    if snap["games"]:
        await s.execute(update(Game).where(Game.id.in_(snap["games"])).values(
            vod_id=b.id, start_time=Game.start_time - offset, end_time=Game.end_time - offset))
    await _set_emotes(s, a.id, await _emotes(s, a.id), snap["targetEmotes"])
    _restore(b, snap["source"])
    b.merged_into = None


async def _unsplit_rows(s: AsyncSession, splice: VodSplice, a: Vod, new: Vod) -> None:
    cut = int(splice.offset_s)
    await s.execute(update(Log).where(Log.vod_id == new.id).values(
        vod_id=a.id, content_offset_seconds=Log.content_offset_seconds + cut))
    await s.execute(update(Game).where(Game.vod_id == new.id).values(
        vod_id=a.id, start_time=Game.start_time + cut, end_time=Game.end_time + cut))
    await _set_emotes(s, new.id, await _emotes(s, new.id), None)
    await s.delete(new)


async def unmerge(target_id: str, source_id: str, force: bool = False) -> dict[str, Any]:
    async with _session() as s, s.begin():
        splice = (await s.execute(
            select(VodSplice).where(VodSplice.kind == "merge", VodSplice.vod_id == target_id,
                                    VodSplice.other_id == source_id, VodSplice.undone_at.is_(None))
        )).scalar_one_or_none()
        if splice is None:
            raise SpliceError(404, f"{source_id} is not merged into {target_id}")
        return await _undo(s, splice, force)


# ── Split ─────────────────────────────────────────────────────────────────


async def _free_id(s: AsyncSession, vod_id: str) -> str:
    taken = set((await s.execute(select(Vod.id).where(Vod.id.startswith(f"{vod_id}-")))).scalars())
    n = 2  # the second half of VOD 123 is 123-2 (Twitch ids are digits only)
    while f"{vod_id}-{n}" in taken:
        n += 1
    return f"{vod_id}-{n}"


def _join(splice: VodSplice) -> tuple[float, float]:
    """Where a merge joined its VODs: its gap chapter, or the offset when there was none."""
    gap = splice.detail.get("gapChapter")
    return (gap["start"], gap["end"]) if gap else (float(splice.offset_s),) * 2


async def split(vod_id: str, at: Any, force: bool = False) -> dict[str, Any]:
    """Split at ``at`` seconds: a merge's join undoes that merge; anywhere else the rest
    becomes a new VOD. Only where no upload has to be cut (409 with the nearest points)."""
    if not timeline.is_number(at):
        raise SpliceError(400, "at must be a number of seconds")
    async with _session() as s, s.begin():
        [a] = await _lock(s, vod_id)
        _not_merged_away(a)
        await _no_active_jobs(s, a.id)
        for merge_ in reversed([sp for sp in await _active(s, a.id) if sp.kind == "merge" and sp.vod_id == a.id]):
            lo, hi = _join(merge_)
            if lo - timeline.SPLIT_SLACK <= at <= hi + timeline.SPLIT_SLACK and await _blocking_splice(s, merge_) is None:
                return {"undid": "merge", **await _undo(s, merge_, force)}

        cut = math.floor(at + 0.5)
        side = _side(a)
        first, second = _plan(timeline.plan_split, side, cut)
        straddling = (await s.execute(select(Game.id).where(
            Game.vod_id == a.id, Game.start_time < cut, Game.end_time > cut))).scalars().all()
        if straddling:
            raise SpliceError(409, f"Per-game upload(s) {', '.join(map(str, straddling))} span {cut}s and cannot "
                                   "be split", games=list(straddling))

        new_id = await _free_id(s, a.id)
        before = _fields(a)
        new = Vod(id=new_id, title=a.title, created_at=a.created_at + dt.timedelta(seconds=cut),
                  duration=format_hhmmss(second.duration), chapters=second.chapters, youtube=second.youtube,
                  drive=second.drive, platform=a.platform, chapters_locked=True,
                  thumbnail_url=next((e.get("thumbnail_url") for e in second.youtube if e.get("thumbnail_url")),
                                     a.thumbnail_url))
        s.add(new)
        await s.flush()  # games and emotes rows reference it
        await _set_emotes(s, new_id, None, _emote_values(await _emotes(s, a.id)))
        games = sorted((await s.execute(update(Game).where(Game.vod_id == a.id, Game.start_time >= cut).values(
            vod_id=new_id, start_time=Game.start_time - cut, end_time=Game.end_time - cut,
        ).returning(Game.id))).scalars())
        comments = (await s.execute(update(Log).where(Log.vod_id == a.id, Log.content_offset_seconds >= cut).values(
            vod_id=new_id, content_offset_seconds=Log.content_offset_seconds - cut))).rowcount
        repointed = await _repoint(s, a.id, new_id, -cut, at_least=cut)

        _apply(a, first)

        splice = VodSplice(kind="split", vod_id=a.id, other_id=new_id, offset_s=cut, detail={
            **first.detail, "movedComments": comments, "movedGames": games, "repointed": sorted(repointed),
        }, snapshot={"target": before, "games": games, "repointed": repointed,
                     "result": {a.id: _fields(a), new_id: _fields(new)}})
        s.add(splice)
        await _rows_moved(s, a.id, new_id)
        await s.flush()
        return {"splice": splice_json(splice), "newVodId": new_id}


async def unsplit(vod_id: str, other_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Undo a split of ``vod_id`` (the latest, or the one that made ``other_id``)."""
    async with _session() as s, s.begin():
        stmt = select(VodSplice).where(VodSplice.kind == "split", VodSplice.vod_id == vod_id,
                                       VodSplice.undone_at.is_(None))
        if other_id is not None:
            stmt = stmt.where(VodSplice.other_id == other_id)
        splice = (await s.execute(stmt.order_by(VodSplice.id.desc()).limit(1))).scalar_one_or_none()
        if splice is None:
            raise SpliceError(404, f"{vod_id} has no split to undo" + (f" into {other_id}" if other_id else ""))
        return await _undo(s, splice, force)


# ── Reading ───────────────────────────────────────────────────────────────


async def active_splices(vod_id: str) -> list[dict[str, Any]]:
    """Splices touching ``vod_id`` that are not undone, oldest first, each with ``undoable``."""
    async with _session() as s:
        mine = await _active(s, vod_id)
        # A later splice blocking an undo may only touch the other VOD of one of these.
        near = await _active(s, *{i for sp in mine for i in (sp.vod_id, sp.other_id)}) if mine else []
        return [{**splice_json(sp), "undoable": _blocker(sp, near) is None} for sp in mine]


def _normal_title(title: str | None) -> str:
    return " ".join((title or "").split()).casefold()


async def merge_candidates(vod_id: str, minutes: int) -> dict[str, Any]:
    """VODs that started after ``vod_id`` and no later than ``minutes`` after it ended."""
    async with _session() as s:
        a = await s.get(Vod, vod_id)
        if a is None:
            raise SpliceError(404, "No Vod Data")
        duration = hhmmss_to_seconds(a.duration)
        ends = a.created_at + dt.timedelta(seconds=duration)
        rows = (await s.execute(
            select(Vod).where(Vod.id != a.id, Vod.merged_into.is_(None), Vod.created_at > a.created_at,
                              Vod.created_at <= ends + dt.timedelta(minutes=minutes))
            .order_by(Vod.created_at).limit(20)
        )).scalars().all()
    out = []
    for v in rows:
        gap = round((v.created_at - a.created_at).total_seconds()) - duration
        out.append({"id": v.id, "streamId": v.stream_id, "title": v.title, "createdAt": iso_utc(v.created_at),
                    "duration": v.duration, "gap": gap, "overlaps": gap < 0,
                    "titlesMatch": _normal_title(v.title) == _normal_title(a.title)})
    return {
        "vod": {"id": a.id, "streamId": a.stream_id, "title": a.title, "createdAt": iso_utc(a.created_at),
                "duration": a.duration, "endsAt": iso_utc(ends), "mergedInto": a.merged_into},
        "withinMinutes": minutes,
        "candidates": out,
    }
