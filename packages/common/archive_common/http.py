"""httpx helpers: one shared client plus a retrying request wrapper."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _retryable(statuses: Iterable[int]):
    statuses = frozenset(statuses)

    def check(exc: BaseException) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in statuses
        return isinstance(exc, httpx.TransportError)

    return check


DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)


async def request(
    method: str,
    url: str,
    *,
    attempts: int = 3,
    retry_statuses: Iterable[int] = DEFAULT_RETRY_STATUSES,
    max_wait: float = 10.0,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, raising ``httpx.HTTPStatusError`` on non-2xx, with retries."""
    http = client or get_client()
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=max_wait),
        retry=retry_if_exception(_retryable(retry_statuses)),
        reraise=True,
    ):
        with attempt:
            resp = await http.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
    raise AssertionError("unreachable")


def status_of(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None
