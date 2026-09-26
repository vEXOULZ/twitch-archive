"""archive-api: read-only HTTP API for the VOD frontend.

Route and response compatibility with the legacy Feathers backend is the
contract; see tests/api_contract.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from archive_common import logs
from archive_common.config import Settings, get_settings
from archive_common.db import get_engine
from archive_common.http import close_client
from archive_common.twitch.helix import Helix

from .comments import Comments
from .errors import FeathersError, LegacyError, bad_literal, legacy_error
from .games_played import games_played
from .invalidation import VodInvalidator
from .middleware import GZIP_MIN_SIZE, JsonBody, RateLimiter, ResponseCache, client_ip
from .services import build_services
from .status import stream_status
from .third_party_emotes import fetch_third_party_emotes

log = logging.getLogger("archive_api")

# Legacy: the limiter covered /vods and the custom routes, not /games /emotes /streams.
RATE_LIMITED_PREFIXES = ("/vods", "/v1/", "/v2/")
SERVICES = ("vods", "games", "emotes", "streams")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = get_engine()
    services = build_services(settings)
    service_cache = ResponseCache(settings.cache_ttl_seconds)
    comments = Comments(ResponseCache(300), ResponseCache(24 * 3600))
    badges_cache = ResponseCache(3600, maxsize=4)
    status_cache = ResponseCache(45, maxsize=1)
    # A complete emote list is kept for hours; one with a failed provider is retried sooner.
    emotes_cache, emotes_partial_cache = ResponseCache(6 * 3600, maxsize=1), ResponseCache(300, maxsize=1)
    limiter = RateLimiter(settings.rate_limit_points, settings.rate_limit_window_seconds)
    helix = Helix(settings)
    # Admin edits reach the public API at once instead of after the cache TTL.
    invalidator = VodInvalidator(settings.database_url, service_cache, status_cache)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        listener = asyncio.create_task(invalidator.run_forever(), name="cache-invalidation")
        yield
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await close_client()
        await engine.dispose()

    app = FastAPI(title="archive-api", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith(RATE_LIMITED_PREFIXES):
            allowed, headers = limiter.hit(client_ip(request))
            if not allowed:
                resp = legacy_error(429, "Too Many Requests")
                resp.headers.update(headers)
                return resp
            response = await call_next(request)
            response.headers.update(headers)
            return response
        return await call_next(request)

    # ── Error handling ────────────────────────────────────────────────────

    @app.exception_handler(FeathersError)
    async def feathers_error(_: Request, exc: FeathersError):
        return exc.response()

    @app.exception_handler(LegacyError)
    async def legacy(_: Request, exc: LegacyError):
        return legacy_error(exc.status, exc.msg)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 405:
            return FeathersError(405, f"Method {request.method} is not allowed").response()
        if exc.status_code == 404:
            return legacy_error(404, "Missing route")
        return legacy_error(exc.status_code, str(exc.detail))

    @app.exception_handler(DBAPIError)
    async def db_error(request: Request, exc: DBAPIError):
        if not bad_literal(exc):
            return await unhandled(request, exc)
        # An invalid literal in a filter (e.g. createdAt[$gte]=garbage)
        log.info("query rejected by database: %s", exc.orig)
        return FeathersError(400, "Invalid query").response()

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return FeathersError(500, "Internal server error").response()

    # ── Health ────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz():
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
        return {"ok": True}

    # ── Feathers services ─────────────────────────────────────────────────

    def register(name: str) -> None:
        svc = services[name]

        async def query(method, arg: str):
            async with engine.connect() as conn:
                return await method(conn, arg)

        @app.get(f"/{name}", name=f"{name}-find")
        async def find(request: Request):
            qs = request.url.query
            body = await service_cache.get_or_render(f"{name}?{qs}", lambda: query(svc.find, qs))
            return body.response(request)

        @app.get(f"/{name}/{{item_id}}", name=f"{name}-get")
        async def get(item_id: str, request: Request):
            body = await service_cache.get_or_render(f"{name}/{item_id}", lambda: query(svc.get, item_id))
            return body.response(request)

        async def disallowed(request: Request):
            raise FeathersError(405, f"Provider 'rest' can not call '{request.method.lower()}'. (disallow)")

        for path in (f"/{name}", f"/{name}/{{item_id}}"):
            app.add_api_route(path, disallowed, methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)

    for name in SERVICES:
        register(name)

    # ── Additions for the new sites (not in the legacy API) ───────────────

    @app.get("/v1/games-played")
    async def games_played_route(request: Request):
        async def fetch() -> list[dict]:
            async with engine.connect() as conn:
                return await games_played(conn)

        return (await service_cache.get_or_render("v1/games-played", fetch)).response(request)

    @app.get("/v1/status")
    async def status_route(request: Request):
        async def fetch() -> dict:
            async with engine.connect() as conn:
                return await stream_status(conn, helix, settings.twitch_id)

        return (await status_cache.get_or_render("status", fetch)).response(request)

    @app.get("/v1/emotes/third-party")
    async def third_party_emotes(request: Request):
        body = emotes_cache.get("emotes") or emotes_partial_cache.get("emotes")
        if body is None:
            value = await fetch_third_party_emotes(settings.twitch_id)
            body = JsonBody(value)
            (emotes_partial_cache if value["failed"] else emotes_cache).set("emotes", body)
        return body.response(request)

    # ── Chat replay ───────────────────────────────────────────────────────

    @app.get("/v1/vods/{vod_id}/comments")
    async def vod_comments(vod_id: str, request: Request):
        params = request.query_params
        body = await comments.handle(engine, vod_id, params.get("content_offset_seconds"), params.get("cursor"))
        return body.response(request)

    # ── Badges ────────────────────────────────────────────────────────────

    @app.get("/v2/badges")
    async def badges(request: Request):
        async def fetch() -> dict:
            if not helix.configured:
                raise LegacyError(500, "Twitch credentials are not configured")
            try:
                channel, glob = await asyncio.gather(helix.channel_badges(settings.twitch_id), helix.global_badges())
            except httpx.HTTPError as exc:
                log.warning("failed to fetch badges: %s", exc)
                raise LegacyError(500, "Something went wrong trying to retrieve channel badges..") from exc
            return {"channel": channel, "global": glob}

        return (await badges_cache.get_or_render("badges", fetch)).response(request)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    logs.setup(settings.log_level)
    uvicorn.run(
        "archive_api.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
        log_level=settings.log_level.lower(),
        log_config=None,  # use the handler from logs.setup
    )


if __name__ == "__main__":
    run()
