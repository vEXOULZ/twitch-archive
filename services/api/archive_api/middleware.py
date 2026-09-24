"""In-process response cache and per-IP rate limiter (replace the legacy Redis ones)."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from cachetools import TTLCache
from starlette.requests import Request


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

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Cached value for ``key``, else ``await factory()`` (not cached if it raises)."""
        value = self.get(key)
        if value is None:
            value = await factory()
            self.set(key, value)
        return value


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
