"""A cached JsonBody must answer exactly like a fresh JSONResponse behind GZipMiddleware."""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from archive_api.middleware import GZIP_MIN_SIZE, JsonBody

VALUES = {
    "small": {"total": 1, "data": [{"id": "1", "title": "é ✓"}]},
    "large": {"total": 500, "data": [{"id": str(i), "title": f"stream {i} — ünïcode"} for i in range(500)]},
}
HEADERS = ("content-type", "content-encoding", "content-length", "vary")


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)
    bodies = {size: JsonBody(value) for size, value in VALUES.items()}

    @app.get("/fresh/{size}")
    async def fresh(size: str):
        return JSONResponse(VALUES[size])

    @app.get("/cached/{size}")
    async def cached(size: str, request: Request):
        return bodies[size].response(request)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


@pytest.mark.parametrize("size", ["small", "large"])
@pytest.mark.parametrize("encoding", ["gzip, deflate", "identity"])
async def test_cached_body_matches_fresh_response(client, size: str, encoding: str) -> None:
    headers = {"accept-encoding": encoding}
    async with client as c:
        fresh = await c.get(f"/fresh/{size}", headers=headers)
        for _ in range(2):  # the second hit reuses the stored gzip bytes
            cached = await c.get(f"/cached/{size}", headers=headers)
            assert cached.content == fresh.content  # decoded body
            assert {h: cached.headers.get(h) for h in HEADERS} == {h: fresh.headers.get(h) for h in HEADERS}
    if size == "large" and encoding.startswith("gzip"):
        assert cached.headers["content-encoding"] == "gzip"
