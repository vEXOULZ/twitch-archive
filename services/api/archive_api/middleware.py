"""In-process response cache and per-IP rate limiter (replace the legacy Redis ones)."""

from __future__ import annotations

import gzip
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from cachetools import TTLCache
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

GZIP_MIN_SIZE = 1024  # shared with the app's GZipMiddleware


class JsonBody:
    """A rendered JSON response body, plus its gzip form made on first use.

    Cached responses are served from these bytes, so a cache hit neither
    re-encodes the JSON nor (for gzip clients) re-compresses it.
    """

    __slots__ = ("raw", "_gzipped")

    def __init__(self, value: Any) -> None:
        self.raw: bytes = JSONResponse(value).body  # byte-identical to an uncached JSONResponse
        self._gzipped: bytes | None = None

    def response(self, request: Request) -> Response:
        if len(self.raw) >= GZIP_MIN_SIZE and "gzip" in request.headers.get("accept-encoding", ""):
            if self._gzipped is None:
                self._gzipped = gzip.compress(self.raw, compresslevel=9)
            # GZipMiddleware passes responses that already carry Content-Encoding through
            # untouched, so set the headers it would have added.
            return Response(self._gzipped, media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(self.raw, media_type="application/json")


class ResponseCache:
    """Tiny TTL cache for read-only responses. The worker writes rarely, so a
    short TTL replaces the legacy purge-on-write logic."""

    def __init__(self, ttl: int, maxsize: int = 4096) -> None:
        self.enabled = ttl > 0
        self._cache: TTLCache[str, Any] = TTLCache(maxsize=maxsize, ttl=max(ttl, 1))

    def get(self, key: str) -> Any:
        return self._cache.get(key) if self.enabled else None

    def set(self, key: str, value: Any) -> None:
        if self.enabled:
            self._cache[key] = value

    def invalidate(self, stale: Callable[[str], bool]) -> None:
        """Drop every entry whose key ``stale`` accepts."""
        for key in [k for k in list(self._cache.keys()) if stale(k)]:
            self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    async def get_or_render(self, key: str, factory: Callable[[], Awaitable[Any]]) -> JsonBody:
        """Cached body for ``key``, else ``await factory()`` rendered (not cached if it raises)."""
        body = self.get(key)
        if body is None:
            body = JsonBody(await factory())
            self.set(key, body)
        return body


def client_ip(request: Request) -> str:
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-real-ip")
        or (request.client.host if request.client else "unknown")
    )


class RateLimiter:
    """Fixed window: ``points`` requests per ``window`` seconds per IP."""

    def __init__(self, points: int, window: int) -> None:
        self.points = points
        self.window = window
        self._hits: TTLCache[str, list[float]] = TTLCache(maxsize=100_000, ttl=window)

    def hit(self, key: str) -> tuple[bool, dict[str, str]]:
        now = time.monotonic()
        entry = self._hits.get(key)
        if entry is None or now - entry[0] >= self.window:
            entry = [now, 0]
        entry[1] += 1
        self._hits[key] = entry
        remaining = max(0, self.points - int(entry[1]))
        reset_in = max(0.0, self.window - (now - entry[0]))
        headers = {
            "Retry-After": str(math.ceil(reset_in)),
            "X-RateLimit-Limit": str(self.points),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(math.ceil(time.time() + reset_in)),
        }
        return entry[1] <= self.points, headers
