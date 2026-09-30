"""Admin HTTP API (LAN only, port 3031).

Request bodies follow the legacy admin routes (``vodId``, ``type``, ...);
``platform`` is accepted and ignored (Twitch only). Every long-running action
enqueues a job and answers ``{"error": false, "msg": ..., "jobId": ...}``.

Every /admin route takes either ``Authorization: Bearer <admin_api_key>``
(scripts) or the dashboard's session cookie (see admin_auth), which comes from
the password (local network only) or a Twitch sign-in (see admin_signin). Every
state-changing request that succeeds is written to the audit log (``audit_log``, named as in
archive_common/audit.py): its body, or what the route put in ``request.state.audit_detail`` (e.g. a
VOD edit's before and after). Actions on jobs are audited by the job runtime (and ``JobService`` for
the legacy table's) instead, as every other way of acting on a job is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import secrets
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import delete, func, insert, or_, select, text, tuple_, update
from vex_platform.actor import SYSTEM, Actor

from archive_common import http
from archive_common.audit import AUDIT_LOG, actor_of, legacy_actor, route_entry
from archive_common.audit import write as write_audit
from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Game, Log, Stream, Vod
from archive_common.serialize import EMOTES, GAMES, box_art_template, duration_seconds, vod_json
from archive_common.timeutil import hhmmss_to_seconds, parse_helix_duration

from . import jobs, splices, vod_edits, youtube
from .admin_auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AdminAuth,
    LoginLimiter,
    Session,
    SessionStore,
    client_address,
    parse_networks,
    parse_password_networks,
    password_allowed,
    plain_http,
)
from .admin_signin import (
    CHECK_S,
    ERRORS,
    STATE_COOKIE,
    STATE_TTL_S,
    AuthClient,
    PendingStates,
    SignInError,
    VexoulzAuth,
    safe_next,
    with_admin,
)
from .context import Deps
from .job_rows import ALL_JOBS
from .events import iso_utc
from .runtime_settings import RuntimeSettings
from .storage import Storage, StorageError
from .vods import notify_rows_moved, splice_reason, upsert_vod

log = logging.getLogger(__name__)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_COOKIE_ARGS = {"path": "/", "secure": True, "httponly": True, "samesite": "strict"}
# The password login over plain HTTP (a LAN address with no TLS): a browser drops a Secure cookie
# set over HTTP, so there it goes without. The password only works from the local network anyway.
LAN_SESSION_COOKIE_ARGS = {**SESSION_COOKIE_ARGS, "secure": False}
STATE_COOKIE_ARGS = {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}
AUDITED_PREFIXES = ("/admin/", "/v2/")
JOB_ROUTES = "/admin/jobs"  # audited by the runtime (and JobService), not by the request
YOUTUBE_CHECK_MAX_AGE = 600  # /admin/health refreshes the YouTube token at most this often
RECENT_JOBS = 20  # jobs shown with a VOD
VOD_LIST_MAX = 200  # GET /admin/vods?limit=
# Steps that refetch from Twitch by VOD id; a job with any of them is refused on a merged/split VOD.
TWITCH_STEPS = {"capture", "fetch_vod", "finalize", "chapters", "chat", "emotes", "bot_chat"}


class AdminError(Exception):
    def __init__(self, status: int, msg: str, headers: dict[str, str] | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        self.status, self.msg, self.headers, self.extra = status, msg, headers, extra or {}


def _ok(msg: str, job: Any = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"error": False, "msg": msg, **extra}
    if job is not None:
        out["jobId"] = job.id
    return out


def _step_names(value: Any, msg: str) -> list[str] | None:
    """``pauseBefore``: a list of step names, or None."""
    if value is not None and not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
        raise AdminError(400, msg)
    return value


async def _job_counts(s) -> dict[str, int]:
    counts = dict((await s.execute(select(ALL_JOBS.c.state, func.count()).group_by(ALL_JOBS.c.state))).all())
    return {st: counts.get(st, 0) for st in jobs.STATES}


def _require(body: dict, *keys: str) -> None:
    for key in keys:
        if body.get(key) in (None, ""):
            raise AdminError(400, f"Missing parameter: {key}")


# Shorthands accepted by GET /admin/jobs?state=
STATE_GROUPS = {
    "waiting": ("queued",),  # will run on its own (possibly after a retry backoff)
    "stopped": ("paused", "failed", "cancelled"),  # needs someone to resume/retry
    "active": jobs.ACTIVE,
    "finished": ("done", "failed", "cancelled"),
}


def _states(value: str) -> list[str]:
    out: list[str] = []
    for name in (v.strip() for v in value.split(",") if v.strip()):
        group = STATE_GROUPS.get(name, (name,))
        for state in group:
            if state not in jobs.STATES:
                raise AdminError(400, f"Unknown state {state!r}; states: {', '.join(jobs.STATES)}, "
                                      f"groups: {', '.join(STATE_GROUPS)}")
            out.append(state)
    return out


def login_of(session: Session) -> str | None:
    """The Twitch login behind a session, for the audit log (None for the password)."""
    return (session.user or {}).get("login") or None


def _audit_json(row: Any) -> dict:
    """An ``audit_log`` row as GET /admin/audit has always shown one (``admin_audit``'s shape)."""
    detail = row.detail
    if row.before is not None or row.after is not None:
        detail = {"before": row.before, "after": row.after}
    return {"id": row.id, "at": iso_utc(row.at), "actor": legacy_actor(row.actor_kind, row.actor_id),
            "actorLogin": row.actor_login, "action": row.action, "target": row.target, "detail": detail}


# Who the request being handled is (set by ``verify``), for the jobs it queues or acts on.
_actor: ContextVar[Actor] = ContextVar("admin_actor", default=SYSTEM)


def _job_json(job: Any) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "vodId": job.vod_id,
        "state": job.state,
        "step": job.step,
        "attempts": job.attempts,
        "lastError": job.last_error,
        "payload": job.payload,
        "notBefore": iso_utc(job.not_before),
        "pauseBefore": job.pause_before,
        "pauseNext": job.pause_next,
        "steps": jobs.KINDS.get(job.kind, []),
        "createdAt": iso_utc(job.created_at),
        "updatedAt": iso_utc(job.updated_at),
    }


def create_admin_app(deps: Deps, service: jobs.JobService, signin: AuthClient | None = None,
                     sessions: SessionStore | None = None, runtime: RuntimeSettings | None = None) -> FastAPI:
    """``signin``: the vexoulz-auth client; by default built from the settings (None when not configured).
    ``sessions``: where dashboard sessions are kept; the worker passes the database's, tests leave memory.
    ``runtime``: the dashboard's setting overrides, already loaded; by default a fresh one over ``deps.settings``."""
    settings = deps.settings
    runtime = runtime or RuntimeSettings(settings)
    storage = Storage(settings.data_dir)
    helix = deps.helix
    app = FastAPI(title="archive-worker admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AdminError)
    async def _admin_error(_req: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse({"error": True, "msg": exc.msg, **exc.extra}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(splices.SpliceError)
    async def _splice_error(req: Request, exc: splices.SpliceError) -> JSONResponse:
        return await _admin_error(req, AdminError(exc.status, exc.msg, extra=exc.extra))

    @app.exception_handler(StorageError)
    async def _storage_error(req: Request, exc: StorageError) -> JSONResponse:
        return await _admin_error(req, AdminError(exc.status, exc.msg))

    @app.exception_handler(jobs.JobNotFound)
    async def _job_not_found(req: Request, _exc: jobs.JobNotFound) -> JSONResponse:
        return await _admin_error(req, AdminError(404, "No such job"))

    @app.exception_handler(jobs.JobConflict)
    async def _job_conflict(req: Request, exc: jobs.JobConflict) -> JSONResponse:
        return await _admin_error(req, AdminError(409, str(exc)))

    @app.exception_handler(jobs.InvalidJob)
    async def _invalid_job(req: Request, exc: jobs.InvalidJob) -> JSONResponse:
        return await _admin_error(req, AdminError(400, str(exc)))

    # ── Auth: API key or session cookie ───────────────────────────────────

    passwords = AdminAuth(settings.admin_password.get_secret_value() or None, store=sessions)
    login_limiter = LoginLimiter()
    trusted_proxies = parse_networks(settings.admin_trusted_proxies)
    password_networks = parse_password_networks(settings.admin_password_networks)
    signin = signin or VexoulzAuth.from_settings(settings)
    twitch_ids = {str(i) for i in settings.admin_twitch_ids}
    pending = PendingStates()

    def session_cookie_args(request: Request) -> dict:
        return LAN_SESSION_COOKIE_ARGS if plain_http(request, trusted_proxies) else SESSION_COOKIE_ARGS
    app.state.admin_sessions = passwords  # for tests
    started_at = dt.datetime.now(dt.timezone.utc)

    async def live_session(token: str | None) -> Session | None:
        """The cookie's session, unless it has expired or its vexoulz-auth session was signed out.
        If vexoulz-auth can't be reached, the session stands and is checked again next time."""
        session = await passwords.session(token)
        if session is None or session.sid is None or signin is None:
            return session
        now = passwords.clock()
        if now - session.checked_at < CHECK_S:
            return session
        try:
            active = await signin.active(session.sid)
        except SignInError as exc:
            log.warning("could not check the admin sign-in: %s", exc)
            return session
        if not active:
            await passwords.logout(token)
            return None
        await passwords.checked(session, now)
        return session

    async def verify(request: Request) -> None:
        header = request.headers.get("authorization")
        if header:
            # Legacy parser: "<anything> <key>"; "Bearer <key>" is the documented form.
            key = header.split(" ", 1)[1] if " " in header else ""
            expected = settings.admin_api_key.get_secret_value()
            if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
                raise AdminError(403, "Not authorized")
            request.state.actor = "api-key"
            _actor.set(actor_of("api-key"))
            return
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise AdminError(403, "Missing auth key")
        session = await live_session(token)
        if session is None:
            raise AdminError(403, "Session expired; log in again")
        if request.method not in SAFE_METHODS and not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
            raise AdminError(403, "Missing or wrong X-CSRF-Token")
        request.state.actor = session.actor
        request.state.actor_login = login_of(session)
        _actor.set(actor_of(session.actor, login_of(session)))

    auth = [Depends(verify)]

    # ── Audit log ─────────────────────────────────────────────────────────

    @app.middleware("http")
    async def audit_log(request: Request, call_next):
        if request.method in SAFE_METHODS or not request.url.path.startswith(AUDITED_PREFIXES):
            return await call_next(request)
        body = await request.body()  # cached by Starlette, so the route can still read it
        response = await call_next(request)
        actor = getattr(request.state, "actor", None)  # set once the request is authenticated
        if actor and response.status_code < 400 and not request.url.path.startswith(JOB_ROUTES):
            try:
                await audit(request, actor, body, getattr(request.state, "actor_login", None))
            except Exception:
                log.exception("could not write the audit log for %s %s", request.method, request.url.path)
        return response

    async def audit(request: Request, actor: str, raw: bytes, actor_login: str | None = None) -> None:
        route = request.scope.get("route")
        params = request.scope.get("path_params") or {}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "password"}
        detail = getattr(request.state, "audit_detail", None)
        target = None
        if "vod_id" in params:
            target = f"vod:{params['vod_id']}"
        elif "job_id" in params:
            target = f"job:{params['job_id']}"
        elif "key" in params:
            target = f"setting:{params['key']}"
        elif "area" in params:
            target = f"storage:{params['area']}/{params.get('name')}"
        elif isinstance(body, dict) and body.get("vodId") not in (None, ""):
            target = f"vod:{body['vodId']}"
        await write_audit(route_entry(f"{request.method} {getattr(route, 'path', request.url.path)}",
                                      actor_of(actor, actor_login), target, body if detail is None else detail))

    # ── Session (browser login) ───────────────────────────────────────────

    def password_here(request: Request) -> bool:
        return passwords.enabled and password_allowed(client_address(request, trusted_proxies), password_networks)

    def session_json(request: Request, session: Session | None) -> dict:
        return {
            "authenticated": session is not None,
            "csrf": session.csrf if session else None,
            "expiresAt": iso_utc(dt.datetime.fromtimestamp(session.expires_at, dt.timezone.utc)) if session else None,
            "passwordLogin": password_here(request),  # offered to this address
            "twitchLogin": signin is not None,
            "user": session.user if session else None,  # the Twitch user; null for a password login
        }

    @app.get("/admin/session")
    async def get_session(request: Request) -> dict:
        return session_json(request, await live_session(request.cookies.get(SESSION_COOKIE)))

    @app.post("/admin/session")
    async def login(request: Request, body: dict | None = Body(None)) -> Response:
        if not passwords.enabled:
            raise AdminError(404, "Password login is off (ARCHIVE_ADMIN_PASSWORD is not set)")
        address = client_address(request, trusted_proxies)
        if not password_allowed(address, password_networks):
            log.warning("admin password refused from %s (not in ARCHIVE_ADMIN_PASSWORD_NETWORKS)", address)
            raise AdminError(403, "The password only works from the local network; sign in with Twitch")
        wait = login_limiter.retry_after(address)
        if wait is not None:
            raise AdminError(429, "Too many failed logins; try again later", {"Retry-After": str(wait)})
        password = (body or {}).get("password")
        if not isinstance(password, str) or not password:
            raise AdminError(400, "Missing parameter: password")
        if not await asyncio.to_thread(passwords.check_password, password):  # scrypt: keep it off the loop
            login_limiter.failed(address)
            log.warning("failed admin login from %s", address)
            raise AdminError(401, "Wrong password")
        login_limiter.reset(address)
        await passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = await passwords.login()
        request.state.actor = "password"
        response = JSONResponse(session_json(request, session))
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **session_cookie_args(request))
        return response

    # ── Twitch sign-in (through vexoulz-auth) ─────────────────────────────
    # The callback is reached through a redirect from another site, which a SameSite=Strict
    # cookie would not be sent on, so the state cookie is Lax. The session cookie stays Strict:
    # it is set here and only read by the dashboard's own requests.

    @app.get("/admin/signin")
    async def signin_start(next: str | None = None, quiet: bool = False) -> Response:
        if signin is None:
            raise AdminError(404, "Twitch sign-in is off (see ARCHIVE_ADMIN_AUTH_* in the README)")
        state = pending.start(safe_next(next), quiet)
        response = RedirectResponse(signin.authorize_url(state), status_code=302)
        response.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL_S, **STATE_COOKIE_ARGS)
        return response

    @app.get("/admin/signin/callback")
    async def signin_callback(request: Request, code: str | None = None, state: str | None = None,
                              error: str | None = None) -> Response:
        if signin is None:
            raise AdminError(404, "Twitch sign-in is off (see ARCHIVE_ADMIN_AUTH_* in the README)")
        cookie = request.cookies.get(STATE_COOKIE) or ""
        started = pending.finish(state)
        # The state must be one this worker handed out, to this browser.
        if started is None or not secrets.compare_digest(cookie.encode(), (state or "").encode()):
            return signin_failed("expired", "/admin")
        next_path, quiet = started.next, started.quiet
        if error or not code:
            return signin_failed(error if error in ERRORS else "twitch", next_path, quiet)
        try:
            signed_in = await signin.redeem(code)
        except SignInError as exc:
            log.warning("admin sign-in failed (%s): %s", exc.reason, exc)
            return signin_failed(exc.reason, next_path, quiet)
        user_id = str(signed_in.user.get("id", ""))
        if user_id not in twitch_ids:
            # A quiet check is every signed-in viewer's first visit: not worth a warning.
            (log.info if quiet else log.warning)(
                "admin sign-in refused for twitch:%s (%s)", user_id, signed_in.user.get("login"))
            return signin_failed("not_allowed", next_path, quiet)
        await passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = await passwords.login(f"twitch:{user_id}", signed_in.user, signed_in.sid)
        try:
            await audit(request, session.actor, b"", login_of(session))
        except Exception:
            log.exception("could not write the audit log for a sign-in")
        response = RedirectResponse(with_admin(next_path, True) if quiet else next_path, status_code=302)
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **SESSION_COOKIE_ARGS)
        response.delete_cookie(STATE_COOKIE, **STATE_COOKIE_ARGS)
        return response

    def signin_failed(reason: str, next_path: str, quiet: bool = False) -> Response:
        """Back to the dashboard's login page, which explains ``auth_error``; a quiet sign-in goes back to
        ``next`` with ``admin=0`` instead."""
        if quiet:
            target = with_admin(next_path, False)
        else:
            target = f"/admin/login?{urlencode({'auth_error': reason, 'next': next_path})}"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(STATE_COOKIE, **STATE_COOKIE_ARGS)
        return response

    @app.delete("/admin/session", status_code=204)
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        session = await passwords.session(token)
        if session is not None:
            if not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
                raise AdminError(403, "Missing or wrong X-CSRF-Token")
            await passwords.logout(token)
            request.state.actor = session.actor
            request.state.actor_login = login_of(session)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, **session_cookie_args(request))
        return response

    def job_action(msg: str, job: Any) -> dict:
        """An admin action on a job: noted in the job's events, answered like any action."""
        service.note(job, msg)
        return _ok(msg, job)

    async def vod_exists(vod_id: str) -> Vod | None:
        async with get_sessionmaker()() as s:
            return await s.get(Vod, str(vod_id))

    async def require_vod(vod_id: str) -> Vod:
        vod = await vod_exists(vod_id)
        if vod is None:
            raise AdminError(404, "No Vod Data")
        return vod

    async def refuse_spliced(vod_id: str, what: str) -> None:
        """Actions that refetch from Twitch by VOD id, or need the VOD's one source video,
        do not fit a merged or split VOD (its row no longer matches Twitch's VOD)."""
        reason = await splice_reason(str(vod_id))
        if reason:
            raise AdminError(409, f"{reason}; {what} would refetch or replace it. Undo the merge/split first")

    async def enqueue(kind: str, vod_id: str | None, payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """``service.enqueue``, refused for a job with a TWITCH_STEPS step on a merged or split VOD."""
        if vod_id is not None and TWITCH_STEPS & set(jobs.KINDS.get(kind, [])):
            await refuse_spliced(vod_id, f"a {kind} job")
        return await service.enqueue(kind, vod_id, payload, actor=_actor.get(), **kwargs)

    def require_helix() -> None:
        if not helix.configured:
            raise AdminError(500, "Twitch client is not configured")

    async def helix_video(vod_id: str) -> dict:
        require_helix()
        video = await helix.get_video(str(vod_id))
        if not video:
            raise AdminError(404, "No Vod Data")
        if str(video.get("user_id")) != str(settings.twitch_id):
            raise AdminError(400, "This vod belongs to another channel..")
        return video

    def vtype(body: dict) -> str:
        t = body.get("type") or "vod"
        if t not in vod_edits.VIDEO_TYPES:
            raise AdminError(400, "type must be 'vod' or 'live'")
        return t

    def type_payload(vod: Vod, body: dict) -> dict:
        t = vtype(body)
        payload: dict[str, Any] = {"type": t}
        if t == "live":
            if not vod.stream_id:
                raise AdminError(400, "Vod has no stream_id; cannot locate the live recording")
            payload["stream_id"] = vod.stream_id
        return payload

    def source_payload(vod: Vod, body: dict) -> dict:
        """type_payload plus an optional local ``path`` to use instead of downloading."""
        payload = type_payload(vod, body)
        if body.get("path"):
            payload["path"] = body["path"]
        return payload

    def single_part(payload: dict, body: dict) -> int:
        part = int(body["part"])
        payload.update(start_part=part, end_part=part)
        return part

    # ── Health / jobs ─────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        async with get_sessionmaker()() as s:
            await s.execute(text("select 1"))
        return {"status": "ok", "runningJobs": service.running}

    async def api_ok() -> bool:
        url = (settings.api_internal_url or f"http://127.0.0.1:{settings.api_port}").rstrip("/")
        try:
            resp = await http.get_client().get(f"{url}/healthz", timeout=3)
            return resp.status_code == 200
        except Exception:  # connection refused, timeout, ...
            return False

    @app.get("/admin/health", dependencies=auth)
    async def health() -> dict:
        """One call for the dashboard's status bar. The YouTube token is refreshed at
        most every 10 minutes here; /admin/youtube/status always refreshes it."""
        async def db_state() -> tuple[dict[str, int], list[Any], Stream | None] | None:
            try:
                async with get_sessionmaker()() as s:
                    counts = await _job_counts(s)
                    failures = list((await s.execute(
                        select(ALL_JOBS).where(ALL_JOBS.c.state == "failed")
                        .order_by(ALL_JOBS.c.updated_at.desc(), ALL_JOBS.c.id.desc()).limit(5)
                    )).all())
                    live = (await s.execute(
                        select(Stream).where(Stream.is_live.is_(True)).order_by(Stream.started_at.desc()).limit(1)
                    )).scalar_one_or_none()
                    return counts, failures, live
            except Exception:
                log.exception("health: database query failed")
                return None

        db, api, yt = await asyncio.gather(db_state(), api_ok(), deps.youtube.cached_check(YOUTUBE_CHECK_MAX_AGE))
        db_ok = db is not None
        counts, failures, live = db or ({st: 0 for st in jobs.STATES}, [], None)
        return {
            "worker": {"ok": db_ok, "runningJobs": service.running, "startedAt": iso_utc(started_at)},
            "api": {"ok": api},
            "youtube": {
                "authorized": yt["authorized"],
                "valid": yt["valid"],
                "error": yt.get("error"),
                "checkedAt": iso_utc(yt["checkedAt"]),
            },
            "live": {
                "live": live is not None,
                "streamId": str(live.id) if live else None,
                "startedAt": iso_utc(live.started_at) if live else None,
            },
            "jobs": {
                "counts": counts,
                "recentFailures": [_job_json(j) for j in failures],
            },
        }

    @app.get("/admin/kinds", dependencies=auth)
    async def list_kinds() -> dict:
        """Job kinds, their steps, and the steps each pauses before by default."""
        return {
            kind: {"steps": steps, "manualSteps": settings.manual_steps.get(kind, [])}
            for kind, steps in jobs.KINDS.items()
        }

    # ── Runtime settings ──────────────────────────────────────────────────

    def settings_json() -> dict:
        return {"data": runtime.describe()}

    async def edited_async(pending) -> Any:
        try:
            return await pending
        except ValueError as exc:
            raise AdminError(400, str(exc)) from exc

    def changed_by(request: Request) -> str:
        return getattr(request.state, "actor_login", None) or request.state.actor

    @app.get("/admin/settings", dependencies=auth)
    async def list_settings() -> dict:
        """Each setting the dashboard can change: value, env default, whether overridden, type, when it applies."""
        return settings_json()

    @app.patch("/admin/settings", dependencies=auth)
    async def patch_settings(request: Request, body: dict = Body(...)) -> dict:
        """``{key: value, ...}``: all of them, or none when one is refused. Audited with before and after."""
        before, after = await edited_async(runtime.update(body, changed_by(request)))
        request.state.audit_detail = {"before": before, "after": after}
        service.apply_settings()  # concurrency, attempts and gates, now
        return settings_json()

    @app.delete("/admin/settings/{key}", dependencies=auth)
    async def reset_setting(key: str, request: Request) -> dict:
        """Back to the env value (or the default)."""
        try:
            before, after = await runtime.reset(key)
        except KeyError:
            raise AdminError(404, f"No setting {key}") from None
        request.state.audit_detail = {"before": before, "after": after}
        service.apply_settings()
        return settings_json()

    # ── Storage ───────────────────────────────────────────────────────────

    @app.get("/admin/storage", dependencies=auth)
    async def get_storage(refresh: bool = False) -> dict:
        """The disk, and each job folder (vods/<id>, live/<stream id>) with its size, VOD, jobs and whether
        it is stale. Sizes are cached for a short while; ``refresh=true`` scans again."""
        return await storage.view(refresh)

    @app.delete("/admin/storage/{area}/{name}", dependencies=auth)
    async def delete_storage(area: str, name: str, request: Request) -> dict:
        """Delete a folder's files; refused while a job for it is queued, running or paused."""
        freed = await storage.delete(area, name)
        request.state.audit_detail = freed
        return freed

    @app.get("/admin/jobs", dependencies=auth)
    async def list_jobs(state: str | None = None, vodId: str | None = None, kind: str | None = None,
                        limit: int = 50, before: int | None = None) -> dict:
        """``state`` takes a comma-separated list of states and/or groups (see STATE_GROUPS).
        Newest first; ``before`` (a job id) pages back through older ones."""
        states = _states(state) if state else None
        c = ALL_JOBS.c
        async with get_sessionmaker()() as s:
            stmt = select(ALL_JOBS).order_by(c.id.desc(), c.legacy).limit(min(max(limit, 1), 500))
            if before is not None:
                stmt = stmt.where(c.id < before)
            if states:
                stmt = stmt.where(c.state.in_(states))
            if vodId:
                stmt = stmt.where(c.vod_id == vodId)
            if kind:
                stmt = stmt.where(c.kind == kind)
            rows = (await s.execute(stmt)).all()
            counts = await _job_counts(s)
        return {"counts": counts, "data": [_job_json(j) for j in rows]}

    @app.post("/admin/jobs", dependencies=auth)
    async def launch_job(body: dict = Body(...)) -> dict:
        """Start any job kind. Body: kind, vodId?, payload?, fromStep?, pauseBefore?, paused?"""
        _require(body, "kind")
        vod_id = str(body["vodId"]) if body.get("vodId") not in (None, "") else None
        if vod_id is not None:
            await require_vod(vod_id)
        pause_before = _step_names(body.get("pauseBefore"), "pauseBefore must be a list of step names")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise AdminError(400, "payload must be an object")
        job = await enqueue(
            str(body["kind"]), vod_id, payload, step=body.get("fromStep") or None,
            pause_before=pause_before, paused=bool(body.get("paused")),
        )
        return _ok(f"Job {job.id} {job.kind} {job.state} at step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/resume", dependencies=auth)
    async def resume_job(job_id: int, body: dict | None = Body(None)) -> dict:
        """Run a paused job from its current step. ``{"once": true}`` pauses again after it."""
        job = await service.resume(job_id, once=bool((body or {}).get("once")), actor=_actor.get())
        return job_action(f"Job {job_id} resumed at step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/pause", dependencies=auth)
    async def pause_job(job_id: int) -> dict:
        job = await service.pause(job_id, actor=_actor.get())
        if job.state == "running":
            return job_action(f"Job {job_id} will pause when step {job.step} finishes", job)
        return job_action(f"Job {job_id} paused at step {job.step}", job)

    @app.get("/admin/jobs/{job_id}", dependencies=auth)
    async def get_job(job_id: int) -> dict:
        return _job_json(await jobs.get(job_id))

    @app.patch("/admin/jobs/{job_id}", dependencies=auth)
    async def patch_job(job_id: int, body: dict = Body(...)) -> dict:
        """``pauseBefore`` (step names, or null for the ARCHIVE_MANUAL_STEPS default) and/or
        ``pauseNext``. Like any gate, they apply when the job next moves on to a step."""
        unknown = sorted(set(body) - {"pauseBefore", "pauseNext"})
        if unknown or not body:
            raise AdminError(400, "Body takes pauseBefore and/or pauseNext" +
                             (f"; unknown: {', '.join(unknown)}" if unknown else ""))
        values: dict[str, Any] = {}
        if "pauseBefore" in body:
            values["pause_before"] = _step_names(
                body["pauseBefore"], "pauseBefore must be a list of step names or null")
        if "pauseNext" in body:
            if not isinstance(body["pauseNext"], bool):
                raise AdminError(400, "pauseNext must be true or false")
            values["pause_next"] = body["pauseNext"]
        job = await service.update(job_id, actor=_actor.get(), **values)
        service.note(job, f"manual steps set: pauseBefore={job.pause_before}, pauseNext={job.pause_next}")
        return _job_json(job)

    @app.get("/admin/jobs/{job_id}/events", dependencies=auth)
    async def job_events(job_id: int, after: int = 0, limit: int = 200) -> dict:
        """The job's log lines, step changes and progress, oldest first. Poll with
        ``after=<next>`` from the previous answer to get only what is new."""
        rows = await service.events(job_id, after=after, limit=min(max(limit, 1), 1000))
        return {"data": rows, "next": rows[-1]["seq"] if rows else after}

    @app.post("/admin/jobs/{job_id}/retry", dependencies=auth)
    async def retry_job(job_id: int) -> dict:
        job = await service.retry(job_id, actor=_actor.get())
        return job_action(f"Job {job_id} re-queued from step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: int) -> dict:
        job = await service.cancel(job_id, actor=_actor.get())
        return job_action(f"Job {job_id} cancelled", job)

    # ── VOD rows ──────────────────────────────────────────────────────────

    @app.post("/admin/generate/vod", dependencies=auth)
    async def generate_vod(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, "Vod data already exists")
        video = await helix_video(body["vodId"])
        await upsert_vod(video)
        await enqueue("chapters", video["id"])
        job = await enqueue("emotes", video["id"])
        return _ok(f"Created vod {video['id']}", job)

    @app.post("/admin/create", dependencies=auth)
    async def create_vod(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "title", "createdAt", "duration")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, f"{body['vodId']} already exists!")
        created = dt.datetime.fromisoformat(str(body["createdAt"]))
        async with get_sessionmaker()() as s:
            s.add(
                Vod(
                    id=str(body["vodId"]),
                    title=body["title"],
                    created_at=created,
                    duration=body["duration"],
                    drive=[body["drive"]] if body.get("drive") else [],
                    platform=body.get("platform") or "twitch",
                )
            )
            await s.commit()
        return _ok(f"Created {body['vodId']} in vods DB!")

    @app.delete("/admin/delete", dependencies=auth)
    async def delete_vod(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        vod_id = str(body["vodId"])
        await refuse_spliced(vod_id, "deleting it (with the chat rows it holds)")
        async with get_sessionmaker()() as s:
            for model in (Log, Emote, Game):
                await s.execute(delete(model).where(model.vod_id == vod_id))
            await s.execute(delete(Vod).where(Vod.id == vod_id))
            await s.commit()
        return _ok(f"Deleted {vod_id} (vod, logs, emotes, games)")

    @app.post("/admin/duration", dependencies=auth)
    async def save_duration(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        await refuse_spliced(body["vodId"], "setting its duration from Twitch")
        video = await helix_video(body["vodId"])
        duration = _helix_hhmmss(video)
        await save_vod(str(body["vodId"]), duration=duration)
        return _ok("Saved duration!", duration=duration)

    # ── VOD editing (dashboard) ───────────────────────────────────────────

    async def save_vod(vod_id: str, **values: Any) -> None:
        """Update a VOD row (a database trigger tells archive-api to drop its cached copies).
        Hiding or showing it also drops its cached chat and emotes, which the trigger doesn't cover."""
        async with get_sessionmaker()() as s:
            res = await s.execute(update(Vod).where(Vod.id == vod_id).values(**values))
            if res.rowcount == 0:
                raise AdminError(404, "No Vod Data")
            if "hidden" in values:
                await notify_rows_moved(s, vod_id)
            await s.commit()

    async def admin_vod(vod_id: str) -> dict:
        """The VOD as GET /vods/{id} renders it, plus what only the dashboard needs."""
        async with get_sessionmaker()() as s:
            vod = await vod_json(await s.connection(), vod_id)
            if vod is None:
                raise AdminError(404, "No Vod Data")
            locked, bot_chat, hidden = (await s.execute(
                select(Vod.chapters_locked, Vod.bot_chat, Vod.hidden).where(Vod.id == vod_id)
            )).one()
            recent = (await s.execute(
                select(ALL_JOBS).where(ALL_JOBS.c.vod_id == vod_id).order_by(ALL_JOBS.c.id.desc()).limit(RECENT_JOBS)
            )).all()
            recent = [_job_json(j) for j in recent]
        return {**vod, "hidden": hidden, "chaptersLocked": locked, "botChat": bot_chat, "jobs": recent,
                "splices": await splices.active_splices(vod_id)}

    def edited(parse, *args) -> Any:
        try:
            return parse(*args)
        except ValueError as exc:
            raise AdminError(400, str(exc)) from exc

    def refuse_merged(vod: Vod, what: str) -> None:
        if vod.merged_into is not None:
            raise AdminError(409, f"{vod.id} was merged into {vod.merged_into.get('id')}; its {what} are that VOD's "
                                  "now. Undo the merge first")

    def vod_row_json(vod: Vod) -> dict:
        """A row of GET /admin/vods, and the fields PATCH changes (named as the public API names them)."""
        return {
            "id": vod.id,
            "title": vod.title,
            "createdAt": iso_utc(vod.created_at),
            "duration": vod.duration,
            "duration_seconds": duration_seconds(vod.duration),
            "thumbnail_url": vod.thumbnail_url,
            "stream_id": vod.stream_id,
            "hidden": vod.hidden,
            "merged_into": vod.merged_into,
        }

    def fields_json(vod: Vod, keys) -> dict:
        row = vod_row_json(vod)
        return {k: row[{"thumbnailUrl": "thumbnail_url"}.get(k, k)] for k in keys}

    @app.get("/admin/vods", dependencies=auth)
    async def list_vods(q: str = "", hidden: bool | None = None, limit: int = 50, before: str | None = None) -> dict:
        """Every VOD, hidden and merged ones too, newest first. ``q`` matches the id exactly or the title
        (case-insensitive substring); ``hidden`` filters; ``before`` (a VOD id, from ``next``) pages back."""
        limit = min(max(limit, 1), VOD_LIST_MAX)
        stmt = select(Vod).order_by(Vod.created_at.desc(), Vod.id.desc()).limit(limit + 1)
        if q.strip():
            term = q.strip()
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(or_(Vod.id == term, Vod.title.ilike(like, escape="\\")))
        if hidden is not None:
            stmt = stmt.where(Vod.hidden.is_(hidden))
        async with get_sessionmaker()() as s:
            if before:
                after = await s.get(Vod, before)
                if after is None:
                    raise AdminError(400, f"before: no VOD {before}")
                stmt = stmt.where(tuple_(Vod.created_at, Vod.id) < tuple_(after.created_at, after.id))
            vods = (await s.execute(stmt)).scalars().all()
        more = len(vods) > limit
        vods = vods[:limit]
        return {"data": [vod_row_json(v) for v in vods], "next": vods[-1].id if more else None}

    @app.get("/admin/vods/{vod_id}", dependencies=auth)
    async def get_vod(vod_id: str) -> dict:
        return await admin_vod(vod_id)

    @app.patch("/admin/vods/{vod_id}", dependencies=auth)
    async def patch_vod(vod_id: str, request: Request, body: dict = Body(...)) -> dict:
        """Any of ``title``, ``hidden``, ``thumbnailUrl`` (null: the default), ``duration`` (HH:MM:SS) and
        ``createdAt`` (ISO). A merged VOD takes only ``hidden``. Audited with the fields before and after."""
        values = edited(vod_edits.vod_fields, body)
        vod = await require_vod(vod_id)
        if set(body) - vod_edits.MERGED_EDITABLE:
            refuse_merged(vod, "contents")
        if "duration" in values:
            await check_fits(vod, hhmmss_to_seconds(values["duration"]))
        before = fields_json(vod, body)
        if values:
            await save_vod(vod.id, **values)
        after = await admin_vod(vod.id)
        request.state.audit_detail = {"before": before, "after": fields_json(await require_vod(vod.id), body)}
        return after

    async def check_fits(vod: Vod, seconds: float) -> None:
        """A new duration must still hold the VOD's chapters and games rows."""
        async with get_sessionmaker()() as s:
            games_end = (await s.execute(select(func.max(Game.end_time)).where(Game.vod_id == vod.id))).scalar()
        for what, end in (("chapters", vod_edits.content_end(vod.chapters)), ("games rows", float(games_end or 0))):
            if end > seconds + vod_edits.DURATION_SLACK:
                raise AdminError(400, f"The {what} run to {end:g}s, past the new duration ({seconds:g}s); "
                                      f"shorten them first")

    async def games_json(vod_id: str) -> list[dict]:
        async with get_sessionmaker()() as s:
            rows = await s.execute(select(*GAMES.columns()).where(GAMES.table.c.vod_id == vod_id)
                                   .order_by(GAMES.table.c.start_time, GAMES.table.c.id))
        return [GAMES.to_json(r) for r in rows.mappings()]

    def game_fields(rows: list[dict]) -> list[dict]:
        """The editable part of each games row (for the audit)."""
        keep = ("start_time", "end_time", *vod_edits.GAME_TEXT, *vod_edits.GAME_URLS)
        return [{k: r.get(k) for k in keep} for r in rows]

    @app.get("/admin/vods/{vod_id}/games", dependencies=auth)
    async def get_games(vod_id: str) -> list[dict]:
        """The VOD's games rows as GET /games renders them, by start."""
        await require_vod(vod_id)
        return await games_json(vod_id)

    @app.put("/admin/vods/{vod_id}/games", dependencies=auth)
    async def put_games(vod_id: str, request: Request, body: dict = Body(...)) -> dict:
        """Replace the games rows: ``{"games": [...]}`` in the shape GET returns (ids and dates ignored)."""
        vod = await require_vod(vod_id)
        refuse_merged(vod, "games rows")
        rows = edited(vod_edits.games, body.get("games"), hhmmss_to_seconds(vod.duration))
        before = await games_json(vod.id)
        async with get_sessionmaker()() as s:
            await s.execute(delete(Game).where(Game.vod_id == vod.id))
            if rows:
                await s.execute(insert(Game), [{"vod_id": vod.id, **r} for r in rows])
            await s.commit()
        request.state.audit_detail = {"before": game_fields(before), "after": game_fields(await games_json(vod.id))}
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/chapters", dependencies=auth)
    async def put_chapters(vod_id: str, body: dict = Body(...)) -> dict:
        """Replace the chapters. ``locked: true`` keeps the automatic chapters step from
        overwriting them (unless that job's payload has ``"force": true``)."""
        vod = await require_vod(vod_id)
        if not isinstance(body.get("locked"), bool):
            raise AdminError(400, "locked must be true or false")
        chapters = edited(vod_edits.chapters, body.get("chapters"), hhmmss_to_seconds(vod.duration))
        await save_vod(vod.id, chapters=chapters, chapters_locked=body["locked"])
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/youtube", dependencies=auth)
    async def put_youtube(vod_id: str, body: dict = Body(...)) -> dict:
        vod = await require_vod(vod_id)
        await save_vod(vod.id, youtube=edited(vod_edits.youtube, body.get("youtube"), vod.youtube))
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/drive", dependencies=auth)
    async def put_drive(vod_id: str, body: dict = Body(...)) -> dict:
        vod = await require_vod(vod_id)
        await save_vod(vod.id, drive=edited(vod_edits.drive, body.get("drive")))
        return await admin_vod(vod.id)

    @app.get("/admin/vods/{vod_id}/emotes", dependencies=auth)
    async def get_vod_emotes(vod_id: str) -> dict | None:
        """The saved emotes row as GET /emotes renders it, or null if none was saved."""
        await require_vod(vod_id)
        async with get_sessionmaker()() as s:
            row = (await s.execute(
                select(*EMOTES.columns()).where(EMOTES.table.c.vod_id == vod_id)
            )).mappings().first()
        return EMOTES.to_json(row) if row else None

    # ── Merging and splitting VODs (see splices) ─────────────────────────

    def force(body: dict) -> bool:
        if body.get("force") not in (None, True, False):
            raise AdminError(400, "force must be true or false")
        return body.get("force") is True

    @app.get("/admin/vods/{vod_id}/merge-candidates", dependencies=auth)
    async def merge_candidates(vod_id: str) -> dict:
        """VODs that started within ARCHIVE_MERGE_CANDIDATE_MINUTES after this one ended."""
        return await splices.merge_candidates(vod_id, settings.merge_candidate_minutes)

    @app.post("/admin/vods/{vod_id}/merge", dependencies=auth)
    async def merge_vods(vod_id: str, body: dict = Body(...)) -> dict:
        """``{"source": id, "gap"?: seconds}``: append ``source`` (the later VOD) to this one."""
        _require(body, "source")
        source = str(body["source"])
        result = await splices.merge(vod_id, source, body.get("gap"))
        return _ok(f"Merged {source} into {vod_id} at {result['splice']['offset']}s", **result,
                   vod=await admin_vod(vod_id))

    @app.post("/admin/vods/{vod_id}/unmerge", dependencies=auth)
    async def unmerge_vods(vod_id: str, body: dict = Body(...)) -> dict:
        """``{"source": id, "force"?: true}``: restore both VODs as they were before the merge."""
        _require(body, "source")
        source = str(body["source"])
        result = await splices.unmerge(vod_id, source, force(body))
        return _ok(f"Unmerged {source} from {vod_id}", **result, vod=await admin_vod(vod_id))

    @app.post("/admin/vods/{vod_id}/split", dependencies=auth)
    async def split_vod(vod_id: str, body: dict = Body(...)) -> dict:
        """``{"at": seconds}``: the rest of the VOD from ``at`` becomes a new VOD, or, at a
        merge's join, that merge is undone. 409 with ``validPoints`` inside an upload."""
        _require(body, "at")
        result = await splices.split(vod_id, body["at"], force(body))
        if result.get("undid") == "merge":
            msg = f"{body['at']}s is where {result['splice']['otherId']} was merged in; undid that merge"
        else:
            msg = f"Split {vod_id} at {result['splice']['offset']}s; the rest is {result['newVodId']}"
        return _ok(msg, **result, vod=await admin_vod(vod_id))

    @app.post("/admin/vods/{vod_id}/unsplit", dependencies=auth)
    async def unsplit_vod(vod_id: str, body: dict | None = Body(None)) -> dict:
        """``{"source"?: second half's id, "force"?: true}``: join the halves again (default: the latest split)."""
        body = body or {}
        other = str(body["source"]) if body.get("source") not in (None, "") else None
        result = await splices.unsplit(vod_id, other, force(body))
        return _ok(f"Joined {result['splice']['otherId']} back into {vod_id}", **result, vod=await admin_vod(vod_id))

    @app.get("/admin/twitch/games", dependencies=auth)
    async def search_games(query: str = "") -> list[dict]:
        """Twitch categories matching ``query`` (for picking a chapter's game)."""
        if not query.strip():
            raise AdminError(400, "Missing parameter: query")
        require_helix()
        return [
            {"gameId": c.get("id"), "name": c.get("name"), "imageTemplate": box_art_template(c.get("box_art_url"))}
            for c in await helix.search_categories(query.strip())
        ]

    # ── Audit log ─────────────────────────────────────────────────────────

    @app.get("/admin/audit", dependencies=auth)
    async def list_audit(before: int | None = None, limit: int = 50) -> dict:
        """Newest first; ``before`` (an entry id) pages back. What admins did: the worker's own actions
        (the monitor queueing a job) are left out."""
        c = AUDIT_LOG.c
        stmt = (select(AUDIT_LOG).where(c.actor_kind.in_(("user", "api_key")))
                .order_by(c.id.desc()).limit(min(max(limit, 1), 500)))
        if before is not None:
            stmt = stmt.where(c.id < before)
        async with get_sessionmaker()() as s:
            rows = (await s.execute(stmt)).all()
        return {"data": [_audit_json(r) for r in rows]}

    # ── Download / upload pipelines ───────────────────────────────────────

    @app.post("/admin/download", dependencies=auth)
    async def download(body: dict = Body(...)) -> dict:
        """Download the whole VOD (or use ``path``), split, upload. Optional part range."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        for src, dst in (("startPart", "start_part"), ("endPart", "end_part")):
            if body.get(src) not in (None, ""):
                payload[dst] = int(body[src])
        job = await enqueue("download", vod.id, payload)
        await enqueue("emotes", vod.id)
        return _ok("Starting download..", job)

    @app.post("/admin/hls/download", dependencies=auth)
    async def hls_download(body: dict = Body(...)) -> dict:
        """Full archive pipeline for a VOD (creates the row from Helix if needed)."""
        _require(body, "vodId")
        vod = await vod_exists(body["vodId"])
        if vod is None:
            await upsert_vod(await helix_video(body["vodId"]))
            vod = await require_vod(body["vodId"])
        if await jobs.find_active("archive", vod_id=vod.id):
            raise AdminError(409, f"An archive job for {vod.id} is already running")
        job = await enqueue("archive", vod.id, {"type": "vod", "stream_id": vod.stream_id})
        return _ok(f"Downloading {vod.id} via HLS..", job)

    @app.post("/admin/reupload", dependencies=auth)
    async def reupload(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "part")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        part = single_part(payload, body)
        job = await enqueue("reupload", vod.id, payload)
        return _ok(f"Re-uploading {vod.id} part {part}..", job)

    @app.post("/admin/dmca", dependencies=auth)
    async def dmca(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        payload["claims"] = body["receivedClaims"]
        job = await enqueue("dmca", vod.id, payload)
        return _ok(f"Muting the DMCA content for {vod.id}...", job)

    @app.post("/admin/part/dmca", dependencies=auth)
    async def part_dmca(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "part", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        payload["claims"] = body["receivedClaims"]
        part = single_part(payload, body)
        job = await enqueue("part_dmca", vod.id, payload)
        return _ok(f"Trimming DMCA Content from {vod.id} Vod Part {part}", job)

    # ── Metadata ──────────────────────────────────────────────────────────

    @app.post("/admin/logs", dependencies=auth)
    async def logs(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        job = await enqueue("chat", str(body["vodId"]))
        return _ok("Getting logs..", job)

    @app.post("/admin/logs/manual", dependencies=auth)
    async def logs_manual(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "path")
        await require_vod(body["vodId"])
        job = await enqueue("logs_manual", str(body["vodId"]), {"path": body["path"]})
        return _ok("Getting logs..", job)

    @app.post("/admin/chapters", dependencies=auth)
    async def chapters(body: dict = Body(...)) -> dict:
        """Chapters from Twitch; ``{"force": true}`` also replaces chapters edited by hand."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        await refuse_spliced(vod.id, "Twitch's chapters")  # before asking Twitch; enqueue would refuse after
        video = await helix_video(vod.id)
        payload: dict[str, Any] = {"duration": parse_helix_duration(video.get("duration", ""))}
        if body.get("force") is True:
            payload["force"] = True
        job = await enqueue("chapters", vod.id, payload)
        return _ok(f"Saving Chapters for {vod.id}", job)

    @app.post("/admin/emotes", dependencies=auth)
    async def emotes(body: dict = Body(...)) -> dict:
        """Fill the VOD's missing emote sets; ``{"force": true}`` replaces the saved ones."""
        _require(body, "vodId")
        await require_vod(body["vodId"])
        force = body.get("force") is True
        job = await enqueue("emotes", str(body["vodId"]), {"force": True} if force else None)
        return _ok("Saving emotes (overwriting).." if force else "Saving emotes..", job)

    async def backfill(kind: str, what: str, body: dict | None) -> dict:
        """One ``kind`` job over every VOD it applies to, or only ``body["vodIds"]``."""
        vod_ids = (body or {}).get("vodIds")
        if vod_ids is not None and not (isinstance(vod_ids, list) and vod_ids):
            raise AdminError(400, "vodIds must be a non-empty list")
        if await jobs.find_active(kind):
            raise AdminError(409, f"A {what} backfill is already running")
        job = await enqueue(kind, None, {"vod_ids": [str(v) for v in vod_ids]} if vod_ids else None)
        return _ok(f"Backfilling {what}..", job)

    @app.post("/admin/emotes/backfill", dependencies=auth)
    async def emotes_backfill(body: dict | None = Body(None)) -> dict:
        """Current global sets onto every emotes row that has none (optionally only ``vodIds``)."""
        return await backfill("global_emotes_backfill", "global emotes", body)

    @app.post("/admin/bot-chat", dependencies=auth)
    async def bot_chat(body: dict = Body(...)) -> dict:
        """Read the VOD's chat from doomtp-bot into bot_logs (adds or updates rows only)."""
        _require(body, "vodId")
        if not settings.doomtp_url:
            raise AdminError(500, "ARCHIVE_DOOMTP_URL is not set")
        vod = await require_vod(body["vodId"])
        if await jobs.find_active("bot_chat", vod_id=vod.id):
            raise AdminError(409, f"A bot chat job for {vod.id} is already running")
        job = await enqueue("bot_chat", vod.id)
        return _ok(f"Reading bot chat for {vod.id}..", job)

    @app.post("/admin/bot-chat/backfill", dependencies=auth)
    async def bot_chat_backfill(body: dict | None = Body(None)) -> dict:
        """Bot chat for every VOD without it (optionally only ``vodIds``); merged or split VODs are skipped."""
        if not settings.doomtp_url:
            raise AdminError(500, "ARCHIVE_DOOMTP_URL is not set")
        return await backfill("bot_chat_backfill", "bot chat", body)

    @app.post("/admin/youtube/parts", dependencies=auth)
    @app.post("/admin/youtube/chapters", dependencies=auth)
    async def youtube_describe(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        job = await enqueue("describe", vod.id, type_payload(vod, body))
        return _ok(f"Updating YouTube descriptions for {vod.id}", job)

    # ── Live recordings (external recorder callback) ──────────────────────

    @app.post("/v2/live", dependencies=auth)
    async def live(body: dict = Body(...)) -> dict:
        _require(body, "streamId", "path")
        stream_id = str(body["streamId"])
        async with get_sessionmaker()() as s:
            vod = (await s.execute(select(Vod).where(Vod.stream_id == stream_id).limit(1))).scalar_one_or_none()
            if vod is None:
                raise AdminError(404, "No Vod found")
            if body.get("driveId"):
                vod.drive = [*(vod.drive or []), {"id": body["driveId"], "type": "live"}]
                await s.commit()
        # Non-200 tells the recorder to delete its file (legacy contract).
        if not settings.multi_track:
            raise AdminError(404, "multiTrack is disabled")
        # Not the guarded enqueue: a refusal here would make the recorder delete its file.
        job = await service.enqueue("live_file", vod.id, {"type": "live", "stream_id": stream_id, "path": body["path"]},
                                    actor=_actor.get())
        return _ok("Starting upload to youtube", job)

    # ── YouTube OAuth ─────────────────────────────────────────────────────

    @app.get("/admin/youtube/auth", dependencies=auth)
    async def youtube_auth(redirect: bool = False):
        if not settings.google_client_id:
            raise AdminError(500, "google_client_id is not configured")
        url = youtube.consent_url(settings)
        if redirect:
            return RedirectResponse(url)
        return {"error": False, "url": url, **await youtube.token_status()}

    @app.get("/admin/youtube/status", dependencies=auth)
    async def youtube_status() -> dict:
        # A real refresh against Google, not just "is a token stored".
        return await deps.youtube.check()

    @app.get("/admin/refreshtoken")
    async def refresh_token(code: str | None = None, state: str | None = None) -> dict:
        # Google redirects the browser here, so this cannot carry the API key;
        # the HMAC-signed ``state`` from /admin/youtube/auth proves the request.
        if not code or not state or not youtube.verify_state(settings, state):
            raise AdminError(403, "Invalid or expired OAuth state; start again at /admin/youtube/auth")
        await youtube.exchange_code(settings, code)
        status = await deps.youtube.check()
        if not status["valid"]:
            raise AdminError(500, f"Token stored but not usable: {status['error']}")
        return _ok("YouTube authorized. You can close this tab.")

    return app
