"""/api/v2's own routes (api_v2.py mounts them): settings, storage and VODs.

The same changes as their /admin routes, in the v2 shapes (snake_case, ``{items, next_cursor}``,
problem details). Each change writes its audit row itself: in the change's transaction where there is
one (settings, VODs), right after it otherwise (a storage delete is files, not rows).

    GET    /api/v2/settings            every runtime setting
    PATCH  /api/v2/settings            {key: value, ...}: all of them or none
    DELETE /api/v2/settings/{key}      back to the env value
    GET    /api/v2/storage             the disk and each job folder
    DELETE /api/v2/storage/{area}/{name}
    GET    /api/v2/vods                every VOD, hidden, merged and synthetic ones too, newest first
    GET    /api/v2/vods/{vod_id}
    PATCH  /api/v2/vods/{vod_id}       title, hidden, thumbnail_url, duration, created_at, tags
    POST   /api/v2/synthetic           a synthetic VOD: segments of real ones (see compose, synthetic)
    GET    /api/v2/synthetic/{vod_id}
    PUT    /api/v2/synthetic/{vod_id}  segments, title, supersedes, tags
    DELETE /api/v2/synthetic/{vod_id}  the whole undo: its sources were never changed
    POST   /api/v2/vods/{vod_id}/merge {source, gap?}: a synthetic VOD of both, superseding them
    POST   /api/v2/vods/{vod_id}/split {at}: two synthetic VODs, superseding it
    GET    /api/v2/playthrough-candidates?game_id=  where real VODs play that game, oldest first
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Callable
from typing import Annotated, Any

from archive_common import audit
from archive_common.db import get_sessionmaker
from archive_common.models import Game, Vod
from archive_common.segments import Segment
from archive_common.serialize import duration_seconds, not_hidden, not_merged_away, real, tagged
from archive_common.timeutil import hhmmss_to_seconds
from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import Field
from sqlalchemy import func, or_, select, tuple_, update
from vex_platform.actor import Actor
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime, decode_cursor, page_of, request_id, utc_iso
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from . import compose, site_tags, splices, synthetic, vod_edits
from .runtime_settings import RuntimeSettings
from .storage import Storage, StorageError
from .vods import notify_rows_moved

Auth = Callable[[Request], Any]
VOD_LIST_MAX = 200


def _actor(request: Request) -> Actor:
    return request.state.actor


def _changed_by(actor: Actor) -> str:
    """``settings.updated_by``, as the v1 routes write it: the login, else the caller."""
    return actor.login or audit.legacy_actor(actor.kind, actor.id)


def _snake(value: Any, deep: bool = True) -> Any:
    """v1's camelCase keys, snake_case (recursively unless ``deep`` is false)."""
    if isinstance(value, dict):
        return {
            "".join(f"_{c.lower()}" if c.isupper() else c for c in k): _snake(v) if deep else v
            for k, v in value.items()
        }
    if isinstance(value, list) and deep:
        return [_snake(v) for v in value]
    return value


# ── Settings ───────────────────────────────────────────────────────────────


class Setting(ApiModel):
    key: str
    value: Any
    default: Any
    overridden: bool
    type: str
    group: str
    applies: str
    help: str
    min: float | None = None
    max: float | None = None
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None
    choices: dict[str, list[str]] | None = None  # the job kinds' steps, for a "steps" setting


def settings_router(runtime: RuntimeSettings, apply: Callable[[], None], auth: Auth) -> APIRouter:
    """``apply``: hands changed settings to the job runtime (concurrency, attempts, gates)."""
    router = APIRouter(tags=["settings"], dependencies=[Depends(auth)])

    def page() -> Page[Setting]:
        # Only the keys: a value may be a dict of job kinds.
        return Page[Setting](items=[Setting(**_snake(item, deep=False)) for item in runtime.describe()])

    @router.get("/settings")
    async def list_settings() -> Page[Setting]:
        """Each setting the dashboard can change: value, env default, whether overridden, when it applies."""
        return page()

    @router.patch("/settings")
    async def update_settings(request: Request, changes: Annotated[dict[str, Any], Body()]) -> Page[Setting]:
        """``{key: value, ...}``: all of them, or none when one is refused. Audited as ``setting.update``."""
        actor = _actor(request)
        try:
            await runtime.update(
                changes,
                _changed_by(actor),
                lambda before, after: AuditEntry(
                    "setting.update", actor, before=before, after=after, request_id=request_id(request)
                ),
            )
        except ValueError as exc:
            raise ApiError(422, "invalid_setting", str(exc)) from None
        apply()
        return page()

    @router.delete("/settings/{key}")
    async def reset_setting(request: Request, key: str) -> Page[Setting]:
        """Back to the env value (or the default). Audited as ``setting.reset``."""
        actor = _actor(request)
        try:
            await runtime.reset(
                key,
                lambda before, after: AuditEntry(
                    "setting.reset", actor, f"setting:{key}", before=before, after=after, request_id=request_id(request)
                ),
            )
        except KeyError:
            raise ApiError(404, "setting_not_found", f"no setting {key}") from None
        apply()
        return page()

    return router


# ── Storage ────────────────────────────────────────────────────────────────


class StorageJob(ApiModel):
    id: int
    kind: str
    state: str
    step: str | None = None
    updated_at: UtcDatetime | None = None


class FolderJobs(ApiModel):
    active: list[StorageJob]
    last: StorageJob | None = None


class FolderVod(ApiModel):
    id: str
    title: str | None = None
    hidden: bool


class Folder(ApiModel):
    area: str
    name: str
    path: str
    bytes: int
    files: int
    modified_at: UtcDatetime | None = None
    vod: FolderVod | None = None
    jobs: FolderJobs
    stale: bool  # no job working in it, and no VOD or its last job failed or was cancelled


class Disk(ApiModel):
    total: int
    used: int
    free: int


class StorageView(ApiModel):
    disk: Disk | None = None
    folders: list[Folder]
    cache_seconds: float


class Freed(ApiModel):
    path: str
    bytes: int
    files: int


def storage_router(storage: Storage, auth: Auth) -> APIRouter:
    router = APIRouter(tags=["storage"], dependencies=[Depends(auth)])

    @router.get("/storage")
    async def get_storage(refresh: bool = False) -> StorageView:
        """The disk, and each job folder (vods/<id>, live/<stream id>) with its size, VOD and jobs.
        Sizes are cached for a short while; ``refresh=true`` scans again."""
        return StorageView(**_snake(await storage.view(refresh)))

    @router.delete("/storage/{area}/{name}")
    async def delete_storage(request: Request, area: str, name: str) -> Freed:
        """Delete a folder's files; refused while a job for it is queued, running or paused.
        Audited as ``storage.delete`` with what it freed."""
        try:
            freed = await storage.delete(area, name)
        except StorageError as exc:
            code = {404: "folder_not_found", 409: "folder_in_use"}.get(exc.status)
            raise ApiError(exc.status, code, exc.msg) from None
        await audit.write(
            AuditEntry(
                "storage.delete",
                _actor(request),
                f"storage:{area}/{name}",
                detail=freed,
                request_id=request_id(request),
            )
        )
        return Freed(**freed)

    return router


# ── VODs ───────────────────────────────────────────────────────────────────


class VodRow(ApiModel):
    id: str
    title: str | None = None
    created_at: UtcDatetime
    duration: str | None = None
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    stream_id: str | None = None
    hidden: bool
    merged_into: dict[str, Any] | None = None  # {id, offset} once merged into that VOD
    tags: list[str]  # [] = a regular VOD
    synthetic: dict[str, Any] | None = None  # {supersedes} on a synthetic VOD


class VodDetail(VodRow):
    platform: str
    chapters: list[dict[str, Any]]
    chapters_locked: bool  # edited by hand: the chapters step leaves them alone
    youtube: list[dict[str, Any]]
    drive: list[dict[str, Any]]
    bot_chat: dict[str, Any] | None = None  # the last bot chat read
    updated_at: UtcDatetime
    splices: list[dict[str, Any]]  # merges and splits touching it, not undone
    segments: list[dict[str, Any]] | None = None  # a synthetic VOD's
    in_synthetic: list[dict[str, Any]]  # the synthetic VODs made with it: {id, title, tags, supersedes, segments}


class VodPatch(ApiModel):
    """Only the fields sent change. ``thumbnail_url: null`` goes back to the default; a merged VOD
    takes only ``hidden`` and ``tags``, a synthetic one only ``title``, ``hidden`` and ``tags``."""

    title: str | None = None
    hidden: bool | None = None
    thumbnail_url: str | None = None
    duration: str | None = None  # HH:MM:SS
    created_at: str | None = None  # ISO 8601 with an offset
    tags: list[str] | None = None


# VodPatch field -> vod_edits.vod_fields' key.
_V1_FIELDS = {"thumbnail_url": "thumbnailUrl", "created_at": "createdAt"}


def _row(vod: Vod) -> dict[str, Any]:
    return {
        "id": vod.id,
        "title": vod.title,
        "created_at": vod.created_at,
        "duration": vod.duration,
        "duration_seconds": duration_seconds(vod.duration),
        "thumbnail_url": vod.thumbnail_url,
        "stream_id": vod.stream_id,
        "hidden": vod.hidden,
        "merged_into": vod.merged_into,
        "tags": list(vod.tags or []),
        "synthetic": vod.synthetic,
    }


async def _detail(vod: Vod) -> VodDetail:
    segments = (await synthetic.get(vod.id))["segments"] if vod.synthetic is not None else None
    return VodDetail(
        **_row(vod),
        platform=vod.platform,
        chapters=vod.chapters or [],
        chapters_locked=vod.chapters_locked,
        youtube=vod.youtube or [],
        drive=vod.drive or [],
        bot_chat=vod.bot_chat,
        updated_at=vod.updated_at,
        splices=await splices.active_splices(vod.id),
        segments=_snake(segments) if segments is not None else None,
        in_synthetic=_snake(await synthetic.containing(vod.id)),
    )


def _audited(vod: Vod, fields: set[str]) -> dict[str, Any]:
    row = _row(vod)
    return {k: utc_iso(row[k]) if k == "created_at" else row[k] for k in sorted(fields)}


def vods_router(auth: Auth) -> APIRouter:
    router = APIRouter(tags=["vods"], dependencies=[Depends(auth)])

    @router.get("/vods")
    async def list_vods(
        q: str = "",
        hidden: bool | None = None,
        tag: str | None = None,
        is_synthetic: Annotated[bool | None, Query(alias="synthetic")] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=VOD_LIST_MAX)] = 50,
    ) -> Page[VodRow]:
        """``q`` matches the id exactly or the title (case-insensitive substring); ``hidden`` and
        ``synthetic`` filter; ``tag`` keeps the VODs with that tag (``tag=`` empty: untagged ones)."""
        stmt = select(Vod).order_by(Vod.created_at.desc(), Vod.id.desc()).limit(limit + 1)
        if term := q.strip():
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(or_(Vod.id == term, Vod.title.ilike(like, escape="\\")))
        if hidden is not None:
            stmt = stmt.where(Vod.hidden.is_(hidden))
        if tag is not None:
            stmt = stmt.where(tagged(tag or None))
        if is_synthetic is not None:
            stmt = stmt.where(~real() if is_synthetic else real())
        if key := decode_cursor(cursor, size=2):
            try:
                at = dt.datetime.fromisoformat(key[0])
            except (TypeError, ValueError):
                raise ApiError(400, "bad_cursor", "The cursor is not one this API issued.") from None
            stmt = stmt.where(tuple_(Vod.created_at, Vod.id) < tuple_(at, str(key[1])))  # type: ignore[arg-type]
        async with get_sessionmaker()() as s:
            vods = (await s.execute(stmt)).scalars().all()
        page = page_of([VodRow(**_row(v)) for v in vods], limit, lambda v: [v.created_at.isoformat(), v.id])
        return Page[VodRow](items=page.items, next_cursor=page.next_cursor)

    @router.get("/vods/{vod_id}")
    async def get_vod(vod_id: str) -> VodDetail:
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, vod_id)
        if vod is None:
            raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
        return await _detail(vod)

    @router.patch("/vods/{vod_id}")
    async def update_vod(request: Request, vod_id: str, patch: VodPatch) -> VodDetail:
        """Audited as ``vod.update`` with the fields sent, before and after."""
        sent = patch.model_dump(include=patch.model_fields_set)
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, vod_id, with_for_update=True)
            if vod is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
            known = await site_tags.vod_tags(s, vod.tags) if "tags" in sent else ()
            try:
                values = vod_edits.vod_fields({_V1_FIELDS.get(k, k): v for k, v in sent.items()}, known)
            except ValueError as exc:
                raise ApiError(422, "invalid_vod", str(exc)) from None
            if vod.merged_into is not None and set(sent) - vod_edits.MERGED_EDITABLE:
                raise ApiError(
                    409,
                    "vod_merged",
                    f"{vod.id} was merged into {vod.merged_into.get('id')}; its "
                    "contents are that VOD's now. Undo the merge first",
                )
            if vod.synthetic is not None and set(sent) - vod_edits.SYNTHETIC_EDITABLE:
                raise ApiError(
                    409,
                    "vod_synthetic",
                    f"{vod.id} is a synthetic VOD: its duration, date and "
                    "thumbnail come from its segments (PUT /api/v2/synthetic)",
                )
            if "duration" in values:
                games_end = (await s.execute(select(func.max(Game.end_time)).where(Game.vod_id == vod.id))).scalar()
                try:
                    vod_edits.check_fits(vod.chapters, float(games_end or 0), hhmmss_to_seconds(values["duration"]))
                except ValueError as exc:
                    raise ApiError(422, "invalid_vod", str(exc)) from None
            before = _audited(vod, set(sent))
            if values:
                # A database trigger tells archive-api to drop its cached copies; hiding or showing it
                # also drops its cached chat and emotes, which the trigger doesn't cover.
                await s.execute(update(Vod).where(Vod.id == vod.id).values(**values))
                if "hidden" in values:
                    await notify_rows_moved(s, vod.id)
                await s.refresh(vod)
            await record(
                s,
                AuditEntry(
                    "vod.update",
                    _actor(request),
                    f"vod:{vod.id}",
                    before=before,
                    after=_audited(vod, set(sent)),
                    request_id=request_id(request),
                ),
                table=audit.TABLE,
            )
            await s.commit()
        return await _detail(vod)

    # ── Synthetic VODs ──

    @router.post("/synthetic", status_code=201)
    async def create_synthetic(request: Request, body: SyntheticCreate) -> SyntheticView:
        """A synthetic VOD of ``segments``: windows ``[start, end)`` of real VODs placed at ``at`` (a
        missing ``at`` follows the segment before). ``supersedes``: the real VODs leave the public lists
        and redirect into it (a merge, a split); untagged, it is listed as a regular VOD. Audited as
        ``synthetic.create``."""
        view = await _synthetic(  # type: ignore[no-untyped-call]
            synthetic.create,
            body.id,
            _segments(body.segments),
            title=body.title,
            supersedes=body.supersedes,
            tags=body.tags,
        )
        await _audit(request, "synthetic.create", body.id, after=view)
        return SyntheticView(**view)

    @router.get("/synthetic/{vod_id}")
    async def get_synthetic(vod_id: str) -> SyntheticView:
        return SyntheticView(**await _synthetic(synthetic.get, vod_id))  # type: ignore[no-untyped-call]

    @router.put("/synthetic/{vod_id}")
    async def change_synthetic(request: Request, vod_id: str, body: SyntheticChange) -> SyntheticView:
        """Only the fields sent change. Audited as ``synthetic.update``, before and after."""
        before, after = await _synthetic(  # type: ignore[no-untyped-call]
            synthetic.change,
            vod_id,
            segments=None if body.segments is None else _segments(body.segments),
            title=body.title,
            supersedes=body.supersedes,
            tags=body.tags,
        )
        before, after = _snake(before), _snake(after)
        await _audit(request, "synthetic.update", vod_id, before=before, after=after)
        return SyntheticView(**after)

    @router.delete("/synthetic/{vod_id}")
    async def delete_synthetic(request: Request, vod_id: str) -> SyntheticView:
        """The synthetic VOD goes; the VODs it was made of are listed (and play) as before. Audited
        as ``synthetic.delete`` with what it was."""
        view = await _synthetic(synthetic.remove, vod_id)  # type: ignore[no-untyped-call]
        await _audit(request, "synthetic.delete", vod_id, before=view)
        return SyntheticView(**view)

    @router.post("/vods/{vod_id}/merge", status_code=201)
    async def merge_vods(request: Request, vod_id: str, body: MergeBody) -> SyntheticView:
        """This VOD then ``source`` (a later one of the same broadcast) as one synthetic VOD
        ``{vod_id}+{source}``, ``source`` placed where it started (or ``gap`` seconds after this one
        ends). Both leave the lists and redirect into it."""
        async with get_sessionmaker()() as s:
            vods = {v.id: v for v in (await s.execute(select(Vod).where(Vod.id.in_([vod_id, body.source])))).scalars()}
        a, b = (_real(vods, i) for i in (vod_id, body.source))
        segments = await _synthetic(compose.merge_segments, synthetic.source_of(a), synthetic.source_of(b), body.gap)  # type: ignore[no-untyped-call]
        view = await _synthetic(synthetic.create, compose.merge_id(a.id, b.id), segments, supersedes=True)  # type: ignore[no-untyped-call]
        await _audit(request, "synthetic.create", view["id"], after=view, detail={"merge": [a.id, b.id]})
        return SyntheticView(**view)

    @router.post("/vods/{vod_id}/split", status_code=201)
    async def split_vod(request: Request, vod_id: str, body: SplitBody) -> list[SyntheticView]:
        """Two synthetic VODs ``{vod_id}-1`` and ``{vod_id}-2``, before and from ``at`` seconds (anywhere,
        mid-upload included); the VOD leaves the lists and redirects into them."""
        async with get_sessionmaker()() as s:
            found = await s.get(Vod, vod_id)
        vod = _real({vod_id: found} if found else {}, vod_id)
        halves = await _synthetic(compose.split_segments, synthetic.source_of(vod), body.at)  # type: ignore[no-untyped-call]
        made: list[dict[str, Any]] = []
        try:
            for new_id, segments in zip(compose.split_ids(vod.id), halves, strict=True):
                made.append(await _synthetic(synthetic.create, new_id, segments, supersedes=True))  # type: ignore[no-untyped-call]
        except ApiError:
            for view in made:  # both or neither
                await synthetic.remove(view["id"])
            raise
        for view in made:
            await _audit(request, "synthetic.create", view["id"], after=view, detail={"split": vod.id, "at": body.at})
        return [SyntheticView(**view) for view in made]

    @router.get("/playthrough-candidates")
    async def playthrough_candidates(game_id: Annotated[str, Query(min_length=1)]) -> Page[PlaythroughWindow]:
        """Every window of a real, shown VOD playing ``game_id`` (its chapters of it, adjacent ones
        joined), oldest first: trim them, then POST them to /synthetic as a playthrough (``tags:
        ["compilation"]``, ``supersedes: false``); a window's ``segment`` is ready to send."""
        async with get_sessionmaker()() as s:
            vods = (
                (
                    await s.execute(
                        select(Vod)
                        .where(real(), not_merged_away(), not_hidden(), Vod.chapters.contains([{"gameId": game_id}]))
                        .order_by(Vod.created_at, Vod.id)
                    )
                )
                .scalars()
                .all()
            )
        items = []
        for vod in vods:
            for start, end in compose.game_windows(vod.chapters, game_id):
                items.append(
                    PlaythroughWindow(
                        vod_id=vod.id,
                        title=vod.title,
                        created_at=vod.created_at,
                        start=start,
                        end=end,
                        length=end - start,
                        segment={"vod_id": vod.id, "start": start, "end": end, "label": f"{vod.created_at:%d %b %Y}"},
                    )
                )
        return Page[PlaythroughWindow](items=items)

    return router


class SegmentIn(ApiModel):
    vod_id: str
    start: float | None = None  # default 0
    end: float | None = None  # default: the source's end
    at: float | None = None  # default: right after the segment before
    label: str | None = None


class SyntheticCreate(ApiModel):
    id: str  # not only digits (those are Twitch's)
    title: str | None = None  # default: the first source's
    supersedes: bool = False
    tags: list[str] = Field(default_factory=list)
    segments: list[SegmentIn]


class SyntheticChange(ApiModel):
    title: str | None = None
    supersedes: bool | None = None
    tags: list[str] | None = None
    segments: list[SegmentIn] | None = None


class SyntheticView(ApiModel):
    id: str
    title: str | None = None
    supersedes: bool
    tags: list[str]
    hidden: bool
    duration: str | None = None
    created_at: UtcDatetime | None = None
    made_at: UtcDatetime | None = None
    changed_at: UtcDatetime | None = None
    segments: list[dict[str, Any]]


class MergeBody(ApiModel):
    source: str
    gap: float | None = None


class SplitBody(ApiModel):
    at: float


class PlaythroughWindow(ApiModel):
    vod_id: str
    title: str | None = None
    created_at: UtcDatetime
    start: float
    end: float
    length: float
    segment: dict[str, Any]


_SYNTHETIC_CODES = {404: "vod_not_found", 409: "synthetic_conflict", 422: "invalid_synthetic"}


def _segments(items: list[SegmentIn]) -> list[Segment]:
    raw = [
        {"vodId": i.vod_id, **i.model_dump(include={"start", "end", "at", "label"}, exclude_none=True)} for i in items
    ]
    try:
        return compose.parse(raw)
    except compose.ComposeError as exc:
        raise ApiError(422, "invalid_synthetic", str(exc)) from None


async def _synthetic(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    """``fn`` (a ``synthetic`` change, or a ``compose`` builder), its refusals as problem details;
    a view (a dict) comes back snake_case."""
    try:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
    except compose.ComposeError as exc:
        raise ApiError(422, "invalid_synthetic", str(exc)) from None
    except synthetic.SyntheticError as exc:
        raise ApiError(
            exc.status, _SYNTHETIC_CODES.get(exc.status, "synthetic_refused"), exc.msg, **exc.extra
        ) from None
    return _snake(result) if isinstance(result, dict) else result


def _real(vods: dict[str, Vod], vod_id: str) -> Vod:
    vod = vods.get(vod_id)
    if vod is None:
        raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
    if vod.synthetic is not None:
        raise ApiError(409, "synthetic_conflict", f"{vod_id} is a synthetic VOD; change its segments instead")
    return vod


async def _audit(request: Request, action: str, vod_id: str, **kwargs: Any) -> None:
    await audit.write(AuditEntry(action, _actor(request), f"vod:{vod_id}", request_id=request_id(request), **kwargs))
