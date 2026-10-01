"""The audit log: ``audit_log``, shared with doomtp-bot (vex-platform's docs/conventions.md, "Audit").

It replaces ``admin_audit`` (Alembic 0005), whose rows are copied in by Alembic 0014 and, for any the
previous release wrote while that migration ran, by ``copy_admin_audit`` when the worker starts. A
copied row keeps its old id as ``request_id = "admin_audit:<id>"``, which is also how a copy knows
what is already there.

The admin API's v1 routes are named here by their route (``ROUTE_ACTIONS``): an ``admin_audit`` row
recorded ``"PATCH /admin/vods/{vod_id}"`` where ``audit_log`` has ``vod.update``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import column, table, text
from vex_platform.actor import Actor
from vex_platform.audit import AuditEntry
from vex_platform.audit.sql import insert_sql, values
from vex_platform.audit.sqlalchemy import record

from .db import get_sessionmaker

TABLE = "public.audit_log"
AUDIT_LOG = table(
    "audit_log",
    *(column(c) for c in ("id", "at", "actor_kind", "actor_id", "actor_login", "via", "action", "target",
                          "scope", "outcome", "before", "after", "detail", "request_id", "job_run_id")),
    schema="public",
)

# "<METHOD> <route>" (admin_audit.action) -> the action audit_log records.
ROUTE_ACTIONS: dict[str, str] = {
    "POST /admin/session": "session.login",
    "GET /admin/signin/callback": "session.signin",
    "DELETE /admin/session": "session.logout",
    "PATCH /admin/settings": "setting.update",
    "DELETE /admin/settings/{key}": "setting.reset",
    "DELETE /admin/storage/{area}/{name}": "storage.delete",
    "POST /admin/jobs": "job.enqueue",
    "POST /admin/jobs/{job_id}/resume": "job.resume",
    "POST /admin/jobs/{job_id}/pause": "job.pause",
    "POST /admin/jobs/{job_id}/retry": "job.retry",
    "POST /admin/jobs/{job_id}/cancel": "job.cancel",
    "PATCH /admin/jobs/{job_id}": "job.update",
    "POST /admin/generate/vod": "vod.generate",
    "POST /admin/create": "vod.create",
    "DELETE /admin/delete": "vod.delete",
    "POST /admin/duration": "vod.duration.refresh",
    "PATCH /admin/vods/{vod_id}": "vod.update",
    "PUT /admin/vods/{vod_id}/games": "vod.games.replace",
    "PUT /admin/vods/{vod_id}/chapters": "vod.chapters.replace",
    "PUT /admin/vods/{vod_id}/youtube": "vod.youtube.replace",
    "PUT /admin/vods/{vod_id}/drive": "vod.drive.replace",
    "POST /admin/vods/{vod_id}/merge": "vod.merge",
    "POST /admin/vods/{vod_id}/unmerge": "vod.unmerge",
    "POST /admin/vods/{vod_id}/split": "vod.split",
    "POST /admin/vods/{vod_id}/unsplit": "vod.unsplit",
    "POST /admin/download": "vod.download",
    "POST /admin/hls/download": "vod.hls_download",
    "POST /admin/reupload": "vod.reupload",
    "POST /admin/dmca": "vod.dmca",
    "POST /admin/part/dmca": "vod.part_dmca",
    "POST /admin/logs": "vod.chat.fetch",
    "POST /admin/logs/manual": "vod.chat.import",
    "POST /admin/chapters": "vod.chapters.fetch",
    "POST /admin/emotes": "vod.emotes.fetch",
    "POST /admin/emotes/backfill": "emotes.backfill",
    "POST /admin/bot-chat": "vod.bot_chat.fetch",
    "POST /admin/bot-chat/backfill": "bot_chat.backfill",
    "POST /admin/youtube/parts": "vod.youtube.describe",
    "POST /admin/youtube/chapters": "vod.youtube.describe",
    "POST /v2/live": "vod.live_file",
}
# A route not in ROUTE_ACTIONS (one since removed, in an old row): the route goes in ``detail``.
UNKNOWN_ROUTE = "admin.request"


def actor_of(actor: str, login: str | None = None) -> Actor:
    """An admin caller as the admin API names it (``request.state.actor``: "api-key", "password" or
    "twitch:<id>") as an ``Actor``."""
    if actor == "api-key":
        return Actor("api_key", "admin", None, "api")
    if actor.startswith("twitch:"):
        return Actor("user", actor.removeprefix("twitch:"), login, "web")
    return Actor("user", actor, login, "web")


def legacy_actor(kind: str, ident: str | None) -> str:
    """``actor_of`` reversed, for GET /admin/audit's ``actor``."""
    if kind == "api_key":
        return "api-key"
    if kind == "user":
        return "password" if ident == "password" else f"twitch:{ident}"
    return kind


def route_entry(route: str, actor: Actor, target: str | None, detail: Any, **extra: Any) -> AuditEntry:
    """The row for a v1 admin request (or an ``admin_audit`` row). A route's ``{"before", "after"}``
    goes in those columns; anything else it recorded (the request body, what a delete freed) in
    ``detail``."""
    action = ROUTE_ACTIONS.get(route)
    if action is None:
        action, detail = UNKNOWN_ROUTE, {"route": route, "detail": detail}
    if isinstance(detail, dict) and set(detail) == {"before", "after"}:
        return AuditEntry(action, actor, target, before=detail["before"], after=detail["after"], **extra)
    return AuditEntry(action, actor, target, detail=detail, **extra)


async def write(entry: AuditEntry) -> int:
    """One row in its own transaction, for a change that has already committed (the v1 routes)."""
    async with get_sessionmaker()() as s:
        row_id = await record(s, entry, table=TABLE)
        await s.commit()
        return row_id


# ── Copying admin_audit ────────────────────────────────────────────────────

_UNCOPIED = text(
    "SELECT a.id, a.at, a.actor, a.actor_login, a.action, a.target, a.detail FROM public.admin_audit a"
    " WHERE NOT EXISTS (SELECT 1 FROM public.audit_log l WHERE l.request_id = 'admin_audit:' || a.id)"
    " ORDER BY a.id"
)
_INSERT = text(insert_sql(TABLE, "named").removesuffix(" RETURNING id"))  # executemany


def _copies(rows: Any) -> list[dict[str, Any]]:
    return [
        values(route_entry(r.action, actor_of(r.actor, r.actor_login), r.target, r.detail,
                           at=r.at, request_id=f"admin_audit:{r.id}"))
        for r in rows
    ]


def copy_admin_audit_sync(conn: Any) -> int:
    """The ``admin_audit`` rows not in ``audit_log`` yet, copied (a SQLAlchemy ``Connection``: Alembic)."""
    params = _copies(conn.execute(_UNCOPIED))
    if params:
        conn.execute(_INSERT, params)
    return len(params)


async def copy_admin_audit() -> int:
    """``copy_admin_audit_sync`` for the worker: rows the previous release wrote after Alembic 0014."""
    async with get_sessionmaker()() as s:
        params = _copies(await s.execute(_UNCOPIED))
        if params:
            await s.execute(_INSERT, params)
            await s.commit()
        return len(params)
