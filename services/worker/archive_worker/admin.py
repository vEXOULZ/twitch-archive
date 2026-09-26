"""Admin HTTP API (LAN only, port 3031).

Request bodies follow the legacy admin routes (``vodId``, ``type``, ...);
``platform`` is accepted and ignored (Twitch only). Every long-running action
enqueues a job and answers ``{"error": false, "msg": ..., "jobId": ...}``.

Every /admin route takes either ``Authorization: Bearer <admin_api_key>``
(scripts) or the dashboard's session cookie (see admin_auth). Every
state-changing request that succeeds is written to the audit log.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
from typing import Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import delete, func, insert, select, text, update

from archive_common import http
from archive_common.db import execute, get_sessionmaker
from archive_common.models import AdminAudit, Emote, Game, Job, Log, Stream, Vod
from archive_common.serialize import EMOTES, box_art_template, vod_json
from archive_common.timeutil import hhmmss_to_seconds, parse_helix_duration

from . import jobs, vod_edits, youtube
from .admin_auth import CSRF_HEADER, SESSION_COOKIE, AdminAuth, LoginLimiter, Session, client_address, parse_networks
from .context import Deps
from .events import event_json, iso_utc
from .vods import upsert_vod

log = logging.getLogger(__name__)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_COOKIE_ARGS = {"path": "/", "secure": True, "httponly": True, "samesite": "strict"}
AUDITED_PREFIXES = ("/admin/", "/v2/")
YOUTUBE_CHECK_MAX_AGE = 600  # /admin/health refreshes the YouTube token at most this often
RECENT_JOBS = 20  # jobs shown with a VOD


class AdminError(Exception):
    def __init__(self, status: int, msg: str, headers: dict[str, str] | None = None) -> None:
        self.status, self.msg, self.headers = status, msg, headers


def _ok(msg: str, job: Job | None = None, **extra: Any) -> dict:
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
    counts = dict((await s.execute(select(Job.state, func.count()).group_by(Job.state))).all())
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


def _audit_json(row: AdminAudit) -> dict:
    return {"id": row.id, "at": iso_utc(row.at), "actor": row.actor, "action": row.action,
            "target": row.target, "detail": row.detail}


def _job_json(job: Job) -> dict:
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


def create_admin_app(deps: Deps, runner: jobs.Runner) -> FastAPI:
    settings = deps.settings
    helix = deps.helix
    app = FastAPI(title="archive-worker admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AdminError)
    async def _admin_error(_req: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse({"error": True, "msg": exc.msg}, status_code=exc.status, headers=exc.headers)

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

    passwords = AdminAuth(settings.admin_password.get_secret_value() or None)
    login_limiter = LoginLimiter()
    trusted_proxies = parse_networks(settings.admin_trusted_proxies)
    events = deps.events
    started_at = dt.datetime.now(dt.timezone.utc)

    def verify(request: Request) -> None:
        header = request.headers.get("authorization")
        if header:
            # Legacy parser: "<anything> <key>"; "Bearer <key>" is the documented form.
            key = header.split(" ", 1)[1] if " " in header else ""
            expected = settings.admin_api_key.get_secret_value()
            if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
                raise AdminError(403, "Not authorized")
            request.state.actor = "api-key"
            return
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise AdminError(403, "Missing auth key")
        session = passwords.session(token)
        if session is None:
            raise AdminError(403, "Session expired; log in again")
        if request.method not in SAFE_METHODS and not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
            raise AdminError(403, "Missing or wrong X-CSRF-Token")
        request.state.actor = "password"

    auth = [Depends(verify)]

    # ── Audit log ─────────────────────────────────────────────────────────

    @app.middleware("http")
    async def audit_log(request: Request, call_next):
        if request.method in SAFE_METHODS or not request.url.path.startswith(AUDITED_PREFIXES):
            return await call_next(request)
        body = await request.body()  # cached by Starlette, so the route can still read it
        response = await call_next(request)
        actor = getattr(request.state, "actor", None)  # set once the request is authenticated
        if actor and response.status_code < 400:
            try:
                await audit(request, actor, body)
            except Exception:
                log.exception("could not write the audit log for %s %s", request.method, request.url.path)
        return response

    async def audit(request: Request, actor: str, raw: bytes) -> None:
        route = request.scope.get("route")
        params = request.scope.get("path_params") or {}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "password"}
        target = None
        if "vod_id" in params:
            target = f"vod:{params['vod_id']}"
        elif "job_id" in params:
            target = f"job:{params['job_id']}"
        elif isinstance(body, dict) and body.get("vodId") not in (None, ""):
            target = f"vod:{body['vodId']}"
        await execute(insert(AdminAudit).values(
            actor=actor,
            action=f"{request.method} {getattr(route, 'path', request.url.path)}",
            target=target,
            detail=body,
        ))

    # ── Session (browser login) ───────────────────────────────────────────

    def session_json(session: Session | None) -> dict:
        return {
            "authenticated": session is not None,
            "csrf": session.csrf if session else None,
            "expiresAt": iso_utc(dt.datetime.fromtimestamp(session.expires_at, dt.timezone.utc)) if session else None,
            "passwordLogin": passwords.enabled,
        }

    @app.get("/admin/session")
    async def get_session(request: Request) -> dict:
        return session_json(passwords.session(request.cookies.get(SESSION_COOKIE)))

    @app.post("/admin/session")
    async def login(request: Request, body: dict | None = Body(None)) -> Response:
        if not passwords.enabled:
            raise AdminError(404, "Password login is off (ARCHIVE_ADMIN_PASSWORD is not set)")
        address = client_address(request, trusted_proxies)
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
        passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = passwords.login()
        request.state.actor = "password"
        response = JSONResponse(session_json(session))
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **SESSION_COOKIE_ARGS)
        return response

    @app.delete("/admin/session", status_code=204)
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        session = passwords.session(token)
        if session is not None:
            if not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
                raise AdminError(403, "Missing or wrong X-CSRF-Token")
            passwords.logout(token)
            request.state.actor = "password"
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, **SESSION_COOKIE_ARGS)
        return response

    def job_action(msg: str, job: Job) -> dict:
        """An admin action on a job: noted in the job's events, answered like any action."""
        events.add(job.id, "info", job.step, msg)
        return _ok(msg, job)

    enqueue = runner.enqueue

    async def vod_exists(vod_id: str) -> Vod | None:
        async with get_sessionmaker()() as s:
            return await s.get(Vod, str(vod_id))

    async def require_vod(vod_id: str) -> Vod:
        vod = await vod_exists(vod_id)
        if vod is None:
            raise AdminError(404, "No Vod Data")
        return vod

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
        return {"status": "ok", "runningJobs": len(runner.running)}

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
        async def db_state() -> tuple[dict[str, int], list[Job], Stream | None] | None:
            try:
                async with get_sessionmaker()() as s:
                    counts = await _job_counts(s)
                    failures = list((await s.execute(
                        select(Job).where(Job.state == "failed").order_by(Job.updated_at.desc(), Job.id.desc()).limit(5)
                    )).scalars())
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
            "worker": {"ok": db_ok, "runningJobs": len(runner.running), "startedAt": iso_utc(started_at)},
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

    @app.get("/admin/jobs", dependencies=auth)
    async def list_jobs(state: str | None = None, vodId: str | None = None, kind: str | None = None,
                        limit: int = 50, before: int | None = None) -> dict:
        """``state`` takes a comma-separated list of states and/or groups (see STATE_GROUPS).
        Newest first; ``before`` (a job id) pages back through older ones."""
        states = _states(state) if state else None
        async with get_sessionmaker()() as s:
            stmt = select(Job).order_by(Job.id.desc()).limit(min(max(limit, 1), 500))
            if before is not None:
                stmt = stmt.where(Job.id < before)
            if states:
                stmt = stmt.where(Job.state.in_(states))
            if vodId:
                stmt = stmt.where(Job.vod_id == vodId)
            if kind:
                stmt = stmt.where(Job.kind == kind)
            rows = (await s.execute(stmt)).scalars().all()
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
        job = await runner.resume(job_id, once=bool((body or {}).get("once")))
        return job_action(f"Job {job_id} resumed at step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/pause", dependencies=auth)
    async def pause_job(job_id: int) -> dict:
        job = await jobs.pause(job_id)
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
        job = await jobs.set_control(job_id, **values)
        events.add(job.id, "info", job.step,
                   f"manual steps set: pauseBefore={job.pause_before}, pauseNext={job.pause_next}")
        return _job_json(job)

    @app.get("/admin/jobs/{job_id}/events", dependencies=auth)
    async def job_events(job_id: int, after: int = 0, limit: int = 200) -> dict:
        """The job's log lines, step changes and progress, oldest first. Poll with
        ``after=<next>`` from the previous answer to get only what is new."""
        async with get_sessionmaker()() as s:
            if (await s.execute(select(Job.id).where(Job.id == job_id))).first() is None:
                raise AdminError(404, "No such job")
        rows = await events.list(job_id, after=after, limit=min(max(limit, 1), 1000))
        return {"data": [event_json(e) for e in rows], "next": rows[-1].id if rows else after}

    @app.post("/admin/jobs/{job_id}/retry", dependencies=auth)
    async def retry_job(job_id: int) -> dict:
        job = await runner.retry(job_id)
        return job_action(f"Job {job_id} re-queued from step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: int) -> dict:
        job = await runner.cancel(job_id)
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
        video = await helix_video(body["vodId"])
        duration = _helix_hhmmss(video)
        await save_vod(str(body["vodId"]), duration=duration)
        return _ok("Saved duration!", duration=duration)

    # ── VOD editing (dashboard) ───────────────────────────────────────────

    async def save_vod(vod_id: str, **values: Any) -> None:
        """Update a VOD row (a database trigger tells archive-api to drop its cached copies)."""
        async with get_sessionmaker()() as s:
            res = await s.execute(update(Vod).where(Vod.id == vod_id).values(**values))
            if res.rowcount == 0:
                raise AdminError(404, "No Vod Data")
            await s.commit()

    async def admin_vod(vod_id: str) -> dict:
        """The VOD as GET /vods/{id} renders it, plus what only the dashboard needs."""
        async with get_sessionmaker()() as s:
            vod = await vod_json(await s.connection(), vod_id)
            if vod is None:
                raise AdminError(404, "No Vod Data")
            locked = (await s.execute(select(Vod.chapters_locked).where(Vod.id == vod_id))).scalar_one()
            recent = (await s.execute(
                select(Job).where(Job.vod_id == vod_id).order_by(Job.id.desc()).limit(RECENT_JOBS)
            )).scalars()
            return {**vod, "chaptersLocked": locked, "jobs": [_job_json(j) for j in recent]}

    def edited(parse, *args) -> Any:
        try:
            return parse(*args)
        except ValueError as exc:
            raise AdminError(400, str(exc)) from exc

    @app.get("/admin/vods/{vod_id}", dependencies=auth)
    async def get_vod(vod_id: str) -> dict:
        return await admin_vod(vod_id)

    @app.patch("/admin/vods/{vod_id}", dependencies=auth)
    async def patch_vod(vod_id: str, body: dict = Body(...)) -> dict:
        unknown = sorted(set(body) - {"title"})
        if unknown:
            raise AdminError(400, f"Only title can be changed here; unknown: {', '.join(unknown)}")
        if "title" in body:
            if not isinstance(body["title"], str) or not body["title"].strip():
                raise AdminError(400, "title must be a non-empty string")
            await save_vod(vod_id, title=body["title"].strip())
        return await admin_vod(vod_id)

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
        """Newest first; ``before`` (an entry id) pages back."""
        stmt = select(AdminAudit).order_by(AdminAudit.id.desc()).limit(min(max(limit, 1), 500))
        if before is not None:
            stmt = stmt.where(AdminAudit.id < before)
        async with get_sessionmaker()() as s:
            rows = (await s.execute(stmt)).scalars().all()
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

    @app.post("/admin/emotes/backfill", dependencies=auth)
    async def emotes_backfill(body: dict | None = Body(None)) -> dict:
        """Current global sets onto every emotes row that has none (optionally only ``vodIds``)."""
        vod_ids = (body or {}).get("vodIds")
        if vod_ids is not None and not (isinstance(vod_ids, list) and vod_ids):
            raise AdminError(400, "vodIds must be a non-empty list")
        if await jobs.find_active("global_emotes_backfill"):
            raise AdminError(409, "A global emotes backfill is already running")
        job = await enqueue("global_emotes_backfill", None, {"vod_ids": [str(v) for v in vod_ids]} if vod_ids else None)
        return _ok("Backfilling global emotes..", job)

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
        job = await enqueue("live_file", vod.id, {"type": "live", "stream_id": stream_id, "path": body["path"]})
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
