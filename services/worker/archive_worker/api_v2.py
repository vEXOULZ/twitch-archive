"""The admin API's /api/v2: vex-platform's conventions (its docs/conventions.md, "API").

snake_case JSON, ISO 8601 UTC times, ``{items, next_cursor}`` lists, RFC 9457 problem details for
errors, and an ``X-Request-ID`` on every response. The same callers as /admin (the API key or the
dashboard's session cookie, see admin.py), but ``request.state.actor`` is an ``Actor`` here; a
refused or failed write is audited (``request.denied``/``request.failed``), and the routes audit the
changes they make themselves.

    /api/v2/jobs ...        vex-platform's job routes, over the runtime's runs (not the legacy table)
    /api/v2/job-kinds
    /api/v2/audit           the audit log, every actor (GET /admin/audit lists admins only)
    /api/v2/settings ...    the admin API's own routes (api_v2_routes.py)
    /api/v2/storage ...
    /api/v2/vods ...
    /api/v2/docs            OpenAPI for these routes, behind the same auth
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from archive_common import audit
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from vex_platform.api import ApiError, RequestIdMiddleware, install_error_handlers
from vex_platform.audit.router import AuditRefusalsMiddleware, audit_router
from vex_platform.jobs.router import jobs_router
from vex_platform.jobs.runtime import JobRuntime

from .job_rows import vod_of
from .vods import splice_reason

PREFIX = "/api/v2"

Auth = Callable[[Request], Awaitable[None]]
VodExists = Callable[[str], Awaitable[Any]]


class GuardedRuntime:
    """The runtime as ``jobs_router`` sees it, with the admin API's checks before a run is queued: a
    ``vod:<id>`` subject must name a VOD, and a kind that refetches from Twitch by VOD id is refused
    on a merged or split one (as POST /admin/jobs refuses it)."""

    def __init__(self, runtime: JobRuntime, vod_exists: VodExists, twitch_steps: set[str]) -> None:
        self._runtime = runtime
        self._vod_exists = vod_exists
        self._twitch_steps = twitch_steps

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def enqueue(
        self, kind: str, subject: str | None = None, payload: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if subject is not None:
            vod_id = vod_of(subject)
            if not vod_id:
                raise ApiError(422, "invalid_subject", "subject must be vod:<id>")
            if await self._vod_exists(vod_id) is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
            job_kind = self._runtime.registry.kinds.get(kind)  # an unknown kind: the runtime refuses it
            if job_kind and self._twitch_steps & set(job_kind.steps):
                if reason := await splice_reason(vod_id):
                    raise ApiError(
                        409,
                        "vod_spliced",
                        f"{reason}; a {kind} job would refetch or replace it. Undo the merge/split first",
                    )
        return await self._runtime.enqueue(kind, subject, payload, **kwargs)


def mount(
    app: FastAPI,
    *,
    auth: Auth,
    runtime: JobRuntime,
    vod_exists: VodExists,
    twitch_steps: set[str],
    routers: Sequence[APIRouter] = (),
) -> None:
    """``auth`` admits a caller and sets ``request.state.actor`` to an ``Actor``, raising ``ApiError``.
    ``routers``: more routes under the prefix (their paths without it)."""
    install_error_handlers(app, PREFIX)
    # Last added runs first: the request id is there for the refusals' audit rows.
    app.add_middleware(AuditRefusalsMiddleware, write=audit.write, prefix=PREFIX)
    app.add_middleware(RequestIdMiddleware)

    v2 = APIRouter(prefix=PREFIX)
    v2.include_router(jobs_router(GuardedRuntime(runtime, vod_exists, twitch_steps), auth))
    v2.include_router(audit_router(runtime.pool.connection, auth, table=audit.TABLE))
    for router in routers:
        v2.include_router(router)
    schema = APIRouter()  # v2 alone, for its OpenAPI (the rest of the app has none)
    schema.include_router(v2)
    docs = APIRouter(prefix=PREFIX, dependencies=[Depends(auth)], include_in_schema=False)

    @docs.get("/openapi.json")
    async def openapi() -> dict[str, Any]:
        return get_openapi(title="archive-worker admin", version="2", routes=schema.routes)

    @docs.get("/docs")
    async def swagger() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=f"{PREFIX}/openapi.json", title="archive-worker admin /api/v2")

    app.include_router(v2)
    app.include_router(docs)
