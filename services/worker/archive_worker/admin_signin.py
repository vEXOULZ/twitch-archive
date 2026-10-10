"""Twitch sign-in for the admin dashboard, through vexoulz-auth (the shared *.vexoul.net sign-in).

The browser goes to vexoulz-auth's ``/authorize`` with a one-time ``state``. It comes back to
``/admin/signin/callback`` with a code, which this worker trades (with its client secret) for the
Twitch user and a vexoulz-auth session id. Only ``ARCHIVE_ADMIN_TWITCH_IDS`` get a dashboard session.
The session id is checked again every ``CHECK_S`` seconds, so "sign out everywhere" on any site
ends the dashboard session too.

A *quiet* sign-in (``/admin/signin?quiet=1``) is the site checking whether someone already signed in
to the site is an admin: vexoulz-auth answers at once for a signed-in browser, and the callback always
goes back to ``next`` with ``admin=1`` (a dashboard session was made) or ``admin=0`` (anything else),
never to the login page.

A *code* sign-in (``POST /admin/session {code}``) needs no redirect at all: the site mints a code with
vexoulz-auth's ``POST /v1/codes`` from the browser and hands it over, and the worker redeems it like
the callback's. Such a code carries the client's first registered redirect URI (``code_redirect_uri``),
not the callback's.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from archive_common.config import Settings

STATE_TTL_S = 600
STATE_COOKIE = "archive_signin"
CHECK_S = 60
#: The ``auth_error`` values the login page knows how to explain.
#: ``misconfigured``: vexoulz-auth refused this worker itself (wrong client secret, unknown client).
ERRORS = ("denied", "expired", "twitch", "not_allowed", "unavailable", "misconfigured")


class SignInError(Exception):
    """vexoulz-auth refused the code or could not be reached. ``reason`` is the ``auth_error`` to show."""

    def __init__(self, msg: str, reason: str = "unavailable") -> None:
        super().__init__(msg)
        self.reason = reason


def _refusal(r: httpx.Response) -> SignInError:
    """Why vexoulz-auth turned down a code: the worker's setup, the code itself, or a passing problem."""
    try:
        code = r.json().get("error")
    except ValueError:
        code = None
    msg = f"vexoulz-auth refused the code ({r.status_code} {code or 'no error code'})"
    if r.status_code == 400 and code == "invalid_grant":
        return SignInError(msg, "expired")  # used, expired, or the vexoulz-auth session ended meanwhile
    if r.status_code == 429 or r.status_code >= 500:
        return SignInError(msg, "unavailable")
    # 401 invalid_client: ARCHIVE_ADMIN_AUTH_CLIENT_ID/SECRET don't match vexoulz-auth's client list.
    return SignInError(msg, "misconfigured")


@dataclass(frozen=True)
class SignedIn:
    user: dict[str, Any]  # {id, login, displayName, avatar, color}
    sid: str


class AuthClient(Protocol):
    def authorize_url(self, state: str) -> str: ...

    async def redeem(self, code: str, fetched: bool = False) -> SignedIn:
        """The user and session behind ``code``; ``fetched``: minted by ``POST /v1/codes``, not
        ``/authorize``. Raises SignInError."""
        ...

    async def active(self, sid: str) -> bool:
        """Whether the vexoulz-auth session is still signed in. Raises SignInError if unsure."""
        ...


class VexoulzAuth:
    def __init__(
        self,
        public_url: str,
        internal_url: str,
        client_id: str,
        secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
        code_redirect_uri: str = "",
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.code_redirect_uri = code_redirect_uri or redirect_uri
        self._http = httpx.AsyncClient(
            base_url=(internal_url or public_url).rstrip("/"),
            auth=(client_id, secret),
            timeout=10.0,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> VexoulzAuth | None:
        secret = settings.admin_auth_client_secret.get_secret_value()
        if not (settings.admin_auth_url and secret and settings.admin_auth_redirect_url and settings.admin_twitch_ids):
            return None
        return cls(
            settings.admin_auth_url,
            settings.admin_auth_internal_url,
            settings.admin_auth_client_id,
            secret,
            settings.admin_auth_redirect_url,
            code_redirect_uri=settings.admin_auth_code_redirect_url,
        )

    def authorize_url(self, state: str) -> str:
        query = urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri, "state": state})
        return f"{self.public_url}/authorize?{query}"

    async def redeem(self, code: str, fetched: bool = False) -> SignedIn:
        redirect_uri = self.code_redirect_uri if fetched else self.redirect_uri
        try:
            r = await self._http.post("/v1/token", json={"code": code, "redirect_uri": redirect_uri})
        except httpx.HTTPError as exc:
            raise SignInError(f"vexoulz-auth unreachable: {exc}") from exc
        if r.status_code != 200:
            raise _refusal(r)
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


class Pending(NamedTuple):
    next: str
    quiet: bool
    expires: float


@dataclass
class PendingStates:
    """Sign-ins on their way through vexoulz-auth: state → where to go afterwards. One use each."""

    ttl_s: float = STATE_TTL_S
    limit: int = 1000
    clock: Callable[[], float] = time.monotonic
    _pending: dict[str, Pending] = field(default_factory=dict, repr=False)

    def start(self, next_path: str, quiet: bool = False) -> str:
        now = self.clock()
        for state in [s for s, p in self._pending.items() if p.expires <= now]:
            del self._pending[state]
        while len(self._pending) >= self.limit:  # oldest first: dicts keep insertion order
            del self._pending[next(iter(self._pending))]
        state = secrets.token_urlsafe(32)
        self._pending[state] = Pending(next_path, quiet, now + self.ttl_s)
        return state

    def finish(self, state: str | None) -> Pending | None:
        """A live state's sign-in, which is used up; None if unknown or expired."""
        entry = self._pending.pop(state or "", None)
        if entry is None or entry.expires <= self.clock():
            return None
        return entry


def with_admin(path: str, ok: bool) -> str:
    """``path`` with ``admin=1`` or ``admin=0`` added: a quiet sign-in's answer."""
    parts = urlsplit(path)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "admin"]
    query.append(("admin", "1" if ok else "0"))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def safe_next(value: str | None) -> str:
    """A path on this site to return to after signing in; anything else becomes ``/admin``."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/admin"
    return value
