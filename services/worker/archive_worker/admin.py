"""Admin HTTP API (LAN only, port 3031).

Request bodies follow the legacy admin routes (``vodId``, ``type``, ...);
``platform`` is accepted and ignored (Twitch only). Every long-running action
enqueues a job and answers ``{"error": false, "msg": ..., "jobId": ...}``.
"""

from __future__ import annotations

import datetime as dt
import hmac
from typing import Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, text, update

from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Game, Job, Log, Vod
from archive_common.twitch.helix import format_hhmmss, parse_helix_duration

from . import jobs, youtube
from .context import Deps
from .monitor import upsert_vod


class AdminError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        self.status, self.msg = status, msg


def _ok(msg: str, job: Job | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"error": False, "msg": msg, **extra}
    if job is not None:
        out["jobId"] = job.id
    return out


def _require(body: dict, *keys: str) -> None:
    for key in keys:
        if body.get(key) in (None, ""):
            raise AdminError(400, f"Missing parameter: {key}")


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
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
    }


def create_admin_app(deps: Deps, runner: jobs.Runner) -> FastAPI:
    settings = deps.settings
    helix = deps.helix
    app = FastAPI(title="archive-worker admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AdminError)
    async def _admin_error(_req: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse({"error": True, "msg": exc.msg}, status_code=exc.status)

    def verify(request: Request) -> None:
        header = request.headers.get("authorization")
        if not header:
            raise AdminError(403, "Missing auth key")
        # Legacy parser: "<anything> <key>"; "Bearer <key>" is the documented form.
        key = header.split(" ", 1)[1] if " " in header else ""
        expected = settings.admin_api_key.get_secret_value()
        if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
            raise AdminError(403, "Not authorized")

    auth = [Depends(verify)]

    async def enqueue(kind: str, vod_id: str | None, payload: dict | None = None) -> Job:
        job = await jobs.enqueue(kind, vod_id, payload)
        runner.poke()
        return job

    async def vod_exists(vod_id: str) -> Vod | None:
        async with get_sessionmaker()() as s:
            return await s.get(Vod, str(vod_id))

    async def require_vod(vod_id: str) -> Vod:
        vod = await vod_exists(vod_id)
        if vod is None:
            raise AdminError(404, "No Vod Data")
        return vod

    async def helix_video(vod_id: str) -> dict:
        if not helix.configured:
            raise AdminError(500, "Twitch client is not configured")
        video = await helix.get_video(str(vod_id))
        if not video:
            raise AdminError(404, "No Vod Data")
        if str(video.get("user_id")) != str(settings.twitch_id):
            raise AdminError(400, "This vod belongs to another channel..")
        return video

    def vtype(body: dict) -> str:
        t = body.get("type") or "vod"
        if t not in ("vod", "live"):
            raise AdminError(400, "type must be 'vod' or 'live'")
        return t

    async def type_payload(vod: Vod, t: str) -> dict:
        payload: dict[str, Any] = {"type": t}
        if t == "live":
            if not vod.stream_id:
                raise AdminError(400, "Vod has no stream_id; cannot locate the live recording")
            payload["stream_id"] = vod.stream_id
        return payload

    # ── Health / jobs ─────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        async with get_sessionmaker()() as s:
            await s.execute(text("select 1"))
        return {"status": "ok", "runningJobs": len(runner.running)}

    @app.get("/admin/jobs", dependencies=auth)
    async def list_jobs(state: str | None = None, vodId: str | None = None, limit: int = 50) -> dict:
        async with get_sessionmaker()() as s:
            stmt = select(Job).order_by(Job.id.desc()).limit(min(max(limit, 1), 500))
            if state:
                stmt = stmt.where(Job.state == state)
            if vodId:
                stmt = stmt.where(Job.vod_id == vodId)
            rows = (await s.execute(stmt)).scalars().all()
        return {"data": [_job_json(j) for j in rows]}

    @app.get("/admin/jobs/{job_id}", dependencies=auth)
    async def get_job(job_id: int) -> dict:
        async with get_sessionmaker()() as s:
            job = await s.get(Job, job_id)
        if job is None:
            raise AdminError(404, "No such job")
        return _job_json(job)

    @app.post("/admin/jobs/{job_id}/retry", dependencies=auth)
    async def retry_job(job_id: int) -> dict:
        job = await jobs.retry(job_id)
        if job is None:
            raise AdminError(404, "No such job")
        runner.poke()
        return _ok(f"Job {job_id} re-queued from step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: int) -> dict:
        job = await jobs.cancel(job_id)
        if job is None:
            raise AdminError(404, "No such job")
        if job.state != "cancelled":
            raise AdminError(409, f"Job is {job.state}; only queued jobs can be cancelled")
        return _ok(f"Job {job_id} cancelled", job)

    # ── VOD rows ──────────────────────────────────────────────────────────

    @app.post("/admin/generate/vod", dependencies=auth)
    async def generate_vod(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, "Vod data already exists")
        video = await helix_video(body["vodId"])
        await upsert_vod(video)
        async with get_sessionmaker()() as s:
            await s.execute(
                update(Vod)
                .where(Vod.id == video["id"])
                .values(duration=format_hhmmss(parse_helix_duration(video.get("duration", ""))),
                        thumbnail_url=video.get("thumbnail_url"))
            )
            await s.commit()
        await enqueue("chapters", video["id"])
        job = await enqueue("emotes", video["id"])
        return _ok(f"Created vod {video['id']}", job)

    @app.post("/admin/create", dependencies=auth)
    async def create_vod(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "title", "createdAt", "duration")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, f"{body['vodId']} already exists!")
        created = dt.datetime.fromisoformat(str(body["createdAt"]).replace("Z", "+00:00"))
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
        duration = format_hhmmss(parse_helix_duration(video.get("duration", "")))
        async with get_sessionmaker()() as s:
            await s.execute(update(Vod).where(Vod.id == str(body["vodId"])).values(duration=duration))
            await s.commit()
        return _ok("Saved duration!", duration=duration)

    # ── Download / upload pipelines ───────────────────────────────────────

    @app.post("/admin/download", dependencies=auth)
    async def download(body: dict = Body(...)) -> dict:
        """Download the whole VOD (or use ``path``), split, upload. Optional part range."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        t = vtype(body)
        payload = await type_payload(vod, t)
        for src, dst in (("path", "path"), ("startPart", "start_part"), ("endPart", "end_part")):
            if body.get(src) not in (None, ""):
                payload[dst] = int(body[src]) if dst != "path" else body[src]
        job = await enqueue("download", vod.id, payload)
        await enqueue("emotes", vod.id)
        return _ok("Starting download..", job)

    @app.post("/admin/hls/download", dependencies=auth)
    async def hls_download(body: dict = Body(...)) -> dict:
        """Full archive pipeline for a VOD (creates the row from Helix if needed)."""
        _require(body, "vodId")
        if not await vod_exists(body["vodId"]):
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
        payload = await type_payload(vod, vtype(body))
        part = int(body["part"])
        payload.update(start_part=part, end_part=part)
        if body.get("path"):
            payload["path"] = body["path"]
        job = await enqueue("reupload", vod.id, payload)
        return _ok(f"Re-uploading {vod.id} part {part}..", job)

    @app.post("/admin/dmca", dependencies=auth)
    async def dmca(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = await type_payload(vod, vtype(body))
        payload["claims"] = body["receivedClaims"]
        if body.get("path"):
            payload["path"] = body["path"]
        job = await enqueue("dmca", vod.id, payload)
        return _ok(f"Muting the DMCA content for {vod.id}...", job)

    @app.post("/admin/part/dmca", dependencies=auth)
    async def part_dmca(body: dict = Body(...)) -> dict:
        _require(body, "vodId", "part", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = await type_payload(vod, vtype(body))
        part = int(body["part"])
        payload.update(claims=body["receivedClaims"], start_part=part, end_part=part)
        if body.get("path"):
            payload["path"] = body["path"]
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
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        video = await helix_video(vod.id)
        payload = {"duration": parse_helix_duration(video.get("duration", ""))}
        job = await enqueue("chapters", vod.id, payload)
        return _ok(f"Saving Chapters for {vod.id}", job)

    @app.post("/admin/emotes", dependencies=auth)
    async def emotes(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        job = await enqueue("emotes", str(body["vodId"]))
        return _ok("Saving emotes..", job)

    @app.post("/admin/youtube/parts", dependencies=auth)
    @app.post("/admin/youtube/chapters", dependencies=auth)
    async def youtube_describe(body: dict = Body(...)) -> dict:
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        job = await enqueue("describe", vod.id, await type_payload(vod, vtype(body)))
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
