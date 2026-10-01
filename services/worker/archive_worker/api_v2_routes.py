"""/api/v2's own routes (api_v2.py mounts them): settings, storage and VODs.

The same changes as their /admin routes, in the v2 shapes (snake_case, ``{items, next_cursor}``,
problem details). Each change writes its audit row itself: in the change's transaction where there is
one (settings, VODs), right after it otherwise (a storage delete is files, not rows).

    GET    /api/v2/settings            every runtime setting
    PATCH  /api/v2/settings            {key: value, ...}: all of them or none
    DELETE /api/v2/settings/{key}      back to the env value
    GET    /api/v2/storage             the disk and each job folder
    DELETE /api/v2/storage/{area}/{name}
    GET    /api/v2/vods                every VOD, hidden and merged ones too, newest first
    GET    /api/v2/vods/{vod_id}
    PATCH  /api/v2/vods/{vod_id}       title, hidden, thumbnail_url, duration, created_at
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import func, or_, select, tuple_, update
from vex_platform.actor import Actor
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime, decode_cursor, page_of, request_id, utc_iso
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from archive_common import audit
from archive_common.db import get_sessionmaker
from archive_common.models import Game, Vod
from archive_common.serialize import duration_seconds
from archive_common.timeutil import hhmmss_to_seconds

from . import splices, vod_edits
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
        return {"".join(f"_{c.lower()}" if c.isupper() else c for c in k): _snake(v) if deep else v
                for k, v in value.items()}
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
            await runtime.update(changes, _changed_by(actor), lambda before, after: AuditEntry(
                "setting.update", actor, before=before, after=after, request_id=request_id(request)))
        except ValueError as exc:
            raise ApiError(422, "invalid_setting", str(exc)) from None
        apply()
        return page()

    @router.delete("/settings/{key}")
    async def reset_setting(request: Request, key: str) -> Page[Setting]:
        """Back to the env value (or the default). Audited as ``setting.reset``."""
        actor = _actor(request)
        try:
            await runtime.reset(key, lambda before, after: AuditEntry(
                "setting.reset", actor, f"setting:{key}", before=before, after=after,
                request_id=request_id(request)))
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
        await audit.write(AuditEntry("storage.delete", _actor(request), f"storage:{area}/{name}", detail=freed,
                                     request_id=request_id(request)))
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


class VodDetail(VodRow):
    platform: str
    chapters: list[dict[str, Any]]
    chapters_locked: bool  # edited by hand: the chapters step leaves them alone
    youtube: list[dict[str, Any]]
    drive: list[dict[str, Any]]
    bot_chat: dict[str, Any] | None = None  # the last bot chat read
    updated_at: UtcDatetime
    splices: list[dict[str, Any]]  # merges and splits touching it, not undone


class VodPatch(ApiModel):
    """Only the fields sent change. ``thumbnail_url: null`` goes back to the default; a merged VOD
    takes only ``hidden``."""
    title: str | None = None
    hidden: bool | None = None
    thumbnail_url: str | None = None
    duration: str | None = None  # HH:MM:SS
    created_at: str | None = None  # ISO 8601 with an offset


# VodPatch field -> vod_edits.vod_fields' key.
_V1_FIELDS = {"thumbnail_url": "thumbnailUrl", "created_at": "createdAt"}


def _row(vod: Vod) -> dict[str, Any]:
    return {"id": vod.id, "title": vod.title, "created_at": vod.created_at, "duration": vod.duration,
            "duration_seconds": duration_seconds(vod.duration), "thumbnail_url": vod.thumbnail_url,
            "stream_id": vod.stream_id, "hidden": vod.hidden, "merged_into": vod.merged_into}


async def _detail(vod: Vod) -> VodDetail:
    return VodDetail(**_row(vod), platform=vod.platform, chapters=vod.chapters or [],
                     chapters_locked=vod.chapters_locked, youtube=vod.youtube or [], drive=vod.drive or [],
                     bot_chat=vod.bot_chat, updated_at=vod.updated_at,
                     splices=await splices.active_splices(vod.id))


def _audited(vod: Vod, fields: set[str]) -> dict[str, Any]:
    row = _row(vod)
    return {k: utc_iso(row[k]) if k == "created_at" else row[k] for k in sorted(fields)}


def vods_router(auth: Auth) -> APIRouter:
    router = APIRouter(tags=["vods"], dependencies=[Depends(auth)])

    @router.get("/vods")
    async def list_vods(q: str = "", hidden: bool | None = None, cursor: str | None = None,
                        limit: Annotated[int, Query(ge=1, le=VOD_LIST_MAX)] = 50) -> Page[VodRow]:
        """``q`` matches the id exactly or the title (case-insensitive substring); ``hidden`` filters."""
        stmt = select(Vod).order_by(Vod.created_at.desc(), Vod.id.desc()).limit(limit + 1)
        if term := q.strip():
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(or_(Vod.id == term, Vod.title.ilike(like, escape="\\")))
        if hidden is not None:
            stmt = stmt.where(Vod.hidden.is_(hidden))
        if key := decode_cursor(cursor, size=2):
            try:
                at = dt.datetime.fromisoformat(key[0])
            except (TypeError, ValueError):
                raise ApiError(400, "bad_cursor", "The cursor is not one this API issued.") from None
            stmt = stmt.where(tuple_(Vod.created_at, Vod.id) < tuple_(at, str(key[1])))
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
        try:
            values = vod_edits.vod_fields({_V1_FIELDS.get(k, k): v for k, v in sent.items()})
        except ValueError as exc:
            raise ApiError(422, "invalid_vod", str(exc)) from None
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, vod_id, with_for_update=True)
            if vod is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
            if vod.merged_into is not None and set(sent) - vod_edits.MERGED_EDITABLE:
                raise ApiError(409, "vod_merged", f"{vod.id} was merged into {vod.merged_into.get('id')}; its "
                                                  "contents are that VOD's now. Undo the merge first")
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
            await record(s, AuditEntry("vod.update", _actor(request), f"vod:{vod.id}", before=before,
                                       after=_audited(vod, set(sent)), request_id=request_id(request)),
                         table=audit.TABLE)
            await s.commit()
        return await _detail(vod)

    return router
