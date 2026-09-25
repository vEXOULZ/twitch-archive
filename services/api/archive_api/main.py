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
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from archive_common import logs
from archive_common.config import Settings, get_settings
from archive_common.db import get_engine
from archive_common.http import close_client
from archive_common.twitch.helix import Helix

from .comments import Comments
from .errors import FeathersError, LegacyError, legacy_error
from .middleware import RateLimiter, ResponseCache, client_ip
from .services import build_services

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
    limiter = RateLimiter(settings.rate_limit_points, settings.rate_limit_window_seconds)
    helix = Helix(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await close_client()
        await engine.dispose()

    app = FastAPI(title="archive-api", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
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
        return JSONResponse({"error": True, "msg": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(DBAPIError)
    async def db_error(_: Request, exc: DBAPIError):
        # Typically an invalid literal in a filter (e.g. createdAt[$gte]=garbage)
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
            return JSONResponse(await service_cache.get_or_set(f"{name}?{qs}", lambda: query(svc.find, qs)))

        @app.get(f"/{name}/{{item_id}}", name=f"{name}-get")
        async def get(item_id: str):
            return JSONResponse(await service_cache.get_or_set(f"{name}/{item_id}", lambda: query(svc.get, item_id)))

        async def disallowed(request: Request):
            raise FeathersError(405, f"Provider 'rest' can not call '{request.method.lower()}'. (disallow)")

        for path in (f"/{name}", f"/{name}/{{item_id}}"):
            app.add_api_route(path, disallowed, methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)

    for name in SERVICES:
        register(name)

    # ── Chat replay ───────────────────────────────────────────────────────

    @app.get("/v1/vods/{vod_id}/comments")
    async def vod_comments(vod_id: str, request: Request):
        params = request.query_params
        async with engine.connect() as conn:
            body = await comments.handle(conn, vod_id, params.get("content_offset_seconds"), params.get("cursor"))
        return JSONResponse(body)

    # ── Badges ────────────────────────────────────────────────────────────

    @app.get("/v2/badges")
    async def badges():
        async def fetch() -> dict:
            if not helix.configured:
                raise LegacyError(500, "Twitch credentials are not configured")
            try:
                channel, glob = await asyncio.gather(helix.channel_badges(settings.twitch_id), helix.global_badges())
            except httpx.HTTPError as exc:
                log.warning("failed to fetch badges: %s", exc)
                raise LegacyError(500, "Something went wrong trying to retrieve channel badges..") from exc
            return {"channel": channel, "global": glob}

        return JSONResponse(await badges_cache.get_or_set("badges", fetch))

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
