"""Browser login for the admin API (same approach as doomtp-bot's web UI).

One admin password (``ARCHIVE_ADMIN_PASSWORD``), hashed with scrypt at startup
and compared in constant time. Sessions live in memory: this is one process, and
a restart logging the dashboard out is the right default for a LAN tool. Without
a password, password login is off and only the API key works.

A session is an ``HttpOnly; Secure; SameSite=Strict`` cookie. Requests that
change state must also send the session's CSRF token in ``X-CSRF-Token``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

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
    created_at: float  # clock() seconds (wall clock, so it can be shown as a date)
    csrf: str
    expires_at: float


@dataclass
class AdminAuth:
    """Password check plus session bookkeeping. ``enabled`` is False when no password is configured."""

    password: str | None = None
    ttl_s: float = SESSION_TTL_S
    clock: Callable[[], float] = time.time
    _salt: bytes = field(default=b"", repr=False)
    _digest: bytes = field(default=b"", repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.password:
            self._salt, self._digest = hash_password(self.password)
        self.password = None  # keep only the hash

    @property
    def enabled(self) -> bool:
        return bool(self._digest)

    def check_password(self, attempt: str) -> bool:
        if not self.enabled:
            return False
        _, digest = hash_password(attempt, self._salt)
        return hmac.compare_digest(digest, self._digest)

    def login(self) -> Session:
        now = self.clock()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[token]
        session = Session(secrets.token_urlsafe(32), now, secrets.token_urlsafe(32), now + self.ttl_s)
        self._sessions[session.token] = session
        return session

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if self.clock() >= session.expires_at:
            self._sessions.pop(token, None)
            return None
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

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


def _in(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def client_address(request: Request, trusted_proxies) -> str:
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
