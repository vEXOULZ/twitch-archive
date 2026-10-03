"""Browser login for the admin API (same approach as doomtp-bot's web UI).

One admin password (``ARCHIVE_ADMIN_PASSWORD``), hashed with scrypt at startup
and compared in constant time. Without a password, password login is off and only
the API key works. A session can also come from Twitch sign-in (see admin_signin);
it then carries the Twitch user and the vexoulz-auth session id it depends on.

Sessions are kept in a ``SessionStore``: the ``admin_sessions`` table in the worker
(``DbSessionStore``, so a restart doesn't sign the dashboard out), or memory in tests.
Only the sha256 of a session's token is stored; the token itself is only in the cookie.

A session is an ``HttpOnly; Secure; SameSite=Strict`` cookie. Requests that
change state must also send the session's CSRF token in ``X-CSRF-Token``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import math
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from archive_common.db import get_sessionmaker
from archive_common.models import AdminSession
from sqlalchemy import delete, update
from starlette.requests import Request

SESSION_COOKIE = "archive_admin"
SESSION_TTL_S = 8 * 3600
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
CSRF_HEADER = "x-csrf-token"


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    return salt, hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)


@dataclass
class Session:
    token: str
    csrf: str
    expires_at: float
    actor: str = "password"  # what the audit log records: "password" or "twitch:<id>"
    user: dict[str, Any] | None = None  # the Twitch user, for a Twitch sign-in
    sid: str | None = None  # the vexoulz-auth session behind it
    checked_at: float = 0.0  # when sid was last confirmed signed in


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore(Protocol):
    """Where sessions are kept, by token hash. ``get`` returns the session with an empty ``token``."""

    async def add(self, key: str, session: Session) -> None: ...

    async def get(self, key: str) -> Session | None: ...

    async def remove(self, key: str) -> None: ...

    async def checked(self, key: str, at: float) -> None: ...

    async def sweep(self, now: float) -> None:
        """Drop the sessions that have expired by ``now``."""
        ...


class MemorySessionStore:
    """Sessions in this process only (tests; a restart signs everyone out)."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def add(self, key: str, session: Session) -> None:
        self._sessions[key] = replace(session, token="")

    async def get(self, key: str) -> Session | None:
        session = self._sessions.get(key)
        return replace(session) if session else None

    async def remove(self, key: str) -> None:
        self._sessions.pop(key, None)

    async def checked(self, key: str, at: float) -> None:
        if key in self._sessions:
            self._sessions[key].checked_at = at

    async def sweep(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[key]


def _utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, dt.UTC)


class DbSessionStore:
    """Sessions in ``admin_sessions`` (Alembic 0009), so they outlast a restart."""

    async def add(self, key: str, session: Session) -> None:
        async with get_sessionmaker()() as s:
            s.add(
                AdminSession(
                    token_hash=key,
                    csrf=session.csrf,
                    actor=session.actor,
                    twitch_user=session.user,
                    sid=session.sid,
                    expires_at=_utc(session.expires_at),
                    checked_at=_utc(session.checked_at),
                )
            )
            await s.commit()

    async def get(self, key: str) -> Session | None:
        async with get_sessionmaker()() as s:
            row = await s.get(AdminSession, key)
        if row is None:
            return None
        return Session(
            "", row.csrf, row.expires_at.timestamp(), row.actor, row.twitch_user, row.sid, row.checked_at.timestamp()
        )

    async def remove(self, key: str) -> None:
        async with get_sessionmaker()() as s:
            await s.execute(delete(AdminSession).where(AdminSession.token_hash == key))
            await s.commit()

    async def checked(self, key: str, at: float) -> None:
        async with get_sessionmaker()() as s:
            await s.execute(update(AdminSession).where(AdminSession.token_hash == key).values(checked_at=_utc(at)))
            await s.commit()

    async def sweep(self, now: float) -> None:
        async with get_sessionmaker()() as s:
            await s.execute(delete(AdminSession).where(AdminSession.expires_at <= _utc(now)))
            await s.commit()


class AdminAuth:
    """Password check plus session bookkeeping. ``enabled`` is False when no password is configured.
    Only the scrypt hash of the password is kept. Sessions go to ``store`` (memory by default)."""

    def __init__(
        self,
        password: str | None = None,
        ttl_s: float = SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
        store: SessionStore | None = None,
    ) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._salt, self._digest = hash_password(password) if password else (b"", b"")
        self.store: SessionStore = store or MemorySessionStore()

    @property
    def enabled(self) -> bool:
        return bool(self._digest)

    def check_password(self, attempt: str) -> bool:
        if not self.enabled:
            return False
        _, digest = hash_password(attempt, self._salt)
        return hmac.compare_digest(digest, self._digest)

    async def login(
        self, actor: str = "password", user: dict[str, Any] | None = None, sid: str | None = None
    ) -> Session:
        now = self.clock()
        await self.store.sweep(now)
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now + self.ttl_s, actor, user, sid, now)
        await self.store.add(token_hash(session.token), session)
        return session

    async def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        key = token_hash(token)
        session = await self.store.get(key)
        if session is None:
            return None
        if self.clock() >= session.expires_at:
            await self.store.remove(key)
            return None
        session.token = token
        return session

    async def checked(self, session: Session, at: float) -> None:
        """Note that the session's vexoulz-auth sign-in was confirmed at ``at``."""
        session.checked_at = at
        await self.store.checked(token_hash(session.token), at)

    async def logout(self, token: str | None) -> None:
        if token:
            await self.store.remove(token_hash(token))

    @staticmethod
    def valid_csrf(session: Session, csrf: str | None) -> bool:
        return bool(csrf) and hmac.compare_digest(session.csrf.encode(), (csrf or "").encode())


@dataclass
class LoginLimiter:
    """At most ``attempts`` failed logins per ``window_s`` seconds per client address."""

    attempts: int = 5
    window_s: float = 300
    clock: Callable[[], float] = time.monotonic
    _failures: defaultdict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), repr=False)

    def _recent(self, key: str) -> deque[float]:
        failures = self._failures[key]
        cutoff = self.clock() - self.window_s
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]  # keep the table to addresses that are actually failing
            return deque()
        return failures

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or None if it may try now."""
        failures = self._recent(key)
        if len(failures) < self.attempts:
            return None
        return max(1, math.ceil(failures[-self.attempts] + self.window_s - self.clock()))

    def failed(self, key: str) -> None:
        self._failures[key].append(self.clock())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def parse_networks(values: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """``ARCHIVE_ADMIN_TRUSTED_PROXIES`` entries (addresses or CIDR ranges); ValueError on a typo."""
    return [ipaddress.ip_network(v.strip(), strict=False) for v in values if v.strip()]


def parse_password_networks(values: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None:
    """``ARCHIVE_ADMIN_PASSWORD_NETWORKS``: as parse_networks, but ``*`` means anywhere (None)."""
    if any(v.strip() == "*" for v in values):
        return None
    return parse_networks(values)


def password_allowed(address: str, networks) -> bool:  # type: ignore[no-untyped-def]
    """Whether the password may be used from ``address`` (see parse_password_networks)."""
    return networks is None or _in(address, networks)


def _in(address: str, networks) -> bool:  # type: ignore[no-untyped-def]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def client_address(request: Request, trusted_proxies) -> str:  # type: ignore[no-untyped-def]
    """The address a login attempt counts against.

    The connecting address, unless it is a trusted proxy: then the nearest
    ``X-Forwarded-For`` hop that is not itself a trusted proxy (hops further left
    were written by the client and prove nothing), else ``X-Real-IP``.
    """
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxies or not _in(peer, trusted_proxies):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in(hop, trusted_proxies):
            return hop
    return request.headers.get("x-real-ip", "").strip() or peer


def plain_http(request: Request, trusted_proxies) -> bool:  # type: ignore[no-untyped-def]
    """Whether the browser reached us over plain HTTP.

    The connection's own scheme, unless it comes from a trusted proxy: then the
    proxy's ``X-Forwarded-Proto`` (its last value, the one the proxy wrote).
    """
    scheme = request.scope.get("scheme", "http")
    peer = request.client.host if request.client else "unknown"
    if trusted_proxies and _in(peer, trusted_proxies):
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-proto", "").split(",") if p.strip()]
        if forwarded:
            scheme = forwarded[-1]
    return scheme.lower() == "http"  # type: ignore[no-any-return]
