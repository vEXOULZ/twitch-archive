"""Twitch sign-in for the admin dashboard, through vexoulz-auth (the shared *.vexoulz.net sign-in).

The browser goes to vexoulz-auth's ``/authorize`` with a one-time ``state``. It comes back to
``/admin/signin/callback`` with a code, which this worker trades (with its client secret) for the
Twitch user and a vexoulz-auth session id. Only ``ARCHIVE_ADMIN_TWITCH_IDS`` get a dashboard session.
The session id is checked again every ``CHECK_S`` seconds, so "sign out everywhere" on any site
ends the dashboard session too.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from archive_common.config import Settings

STATE_TTL_S = 600
STATE_COOKIE = "archive_signin"
CHECK_S = 60
#: The ``auth_error`` values the login page knows how to explain.
ERRORS = ("denied", "expired", "twitch", "not_allowed", "unavailable")


class SignInError(Exception):
    """vexoulz-auth refused the code or could not be reached."""


@dataclass(frozen=True)
class SignedIn:
    user: dict[str, Any]  # {id, login, displayName, avatar, color}
    sid: str


class AuthClient(Protocol):
    def authorize_url(self, state: str) -> str: ...

    async def redeem(self, code: str) -> SignedIn: ...

    async def active(self, sid: str) -> bool:
        """Whether the vexoulz-auth session is still signed in. Raises SignInError if unsure."""
        ...


class VexoulzAuth:
    def __init__(self, public_url: str, internal_url: str, client_id: str, secret: str, redirect_uri: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.public_url = public_url.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(base_url=(internal_url or public_url).rstrip("/"),
                                       auth=(client_id, secret), timeout=10.0, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> VexoulzAuth | None:
        secret = settings.admin_auth_client_secret.get_secret_value()
        if not (settings.admin_auth_url and secret and settings.admin_auth_redirect_url and settings.admin_twitch_ids):
            return None
        return cls(settings.admin_auth_url, settings.admin_auth_internal_url, settings.admin_auth_client_id,
                   secret, settings.admin_auth_redirect_url)

    def authorize_url(self, state: str) -> str:
        query = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri, "state": state})
        return f"{self.public_url}/authorize?{query}"

    async def redeem(self, code: str) -> SignedIn:
        try:
            r = await self._http.post("/v1/token", json={"code": code, "redirect_uri": self.redirect_uri})
        except httpx.HTTPError as exc:
            raise SignInError(f"vexoulz-auth unreachable: {exc}") from exc
        if r.status_code != 200:
            raise SignInError(f"vexoulz-auth refused the code ({r.status_code})")
        body = r.json()
        return SignedIn(body["user"], body["sid"])

    async def active(self, sid: str) -> bool:
        try:
            r = await self._http.get(f"/v1/sessions/{sid}")
        except httpx.HTTPError as exc:
            raise SignInError(f"vexoulz-auth unreachable: {exc}") from exc
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise SignInError(f"vexoulz-auth answered {r.status_code}")
        return bool(r.json().get("active"))


@dataclass
class PendingStates:
    """Sign-ins on their way through vexoulz-auth: state → where to go afterwards. One use each."""

    ttl_s: float = STATE_TTL_S
    limit: int = 1000
    clock: Callable[[], float] = time.monotonic
    _pending: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)

    def start(self, next_path: str) -> str:
        now = self.clock()
        for state in [s for s, (_, exp) in self._pending.items() if exp <= now]:
            del self._pending[state]
        while len(self._pending) >= self.limit:  # oldest first: dicts keep insertion order
            del self._pending[next(iter(self._pending))]
        state = secrets.token_urlsafe(32)
        self._pending[state] = (next_path, now + self.ttl_s)
        return state

    def finish(self, state: str | None) -> str | None:
        """The ``next`` path of a live state, which is used up; None if unknown or expired."""
        entry = self._pending.pop(state or "", None)
        if entry is None or entry[1] <= self.clock():
            return None
        return entry[0]


def safe_next(value: str | None) -> str:
    """A path on this site to return to after signing in; anything else becomes ``/admin``."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/admin"
    return value
