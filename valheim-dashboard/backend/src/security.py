"""Bounded in-memory sessions plus CSRF and exact Host/Origin/JSON checks.

This backend has no separate authentication: reaching the loopback listener
(directly or through an operator's own SSH tunnel) is the access boundary.
Sessions here exist only to bind a CSRF token to a client so that a
state-changing request cannot be forged from another origin or replayed
without a token the client could only have received from this backend.
"""

from http.cookies import SimpleCookie
import os
import secrets
import time
from typing import NamedTuple

SESSION_COOKIE_NAME = "vd_session"
MAX_SESSIONS = 64
SESSION_TTL_SECONDS = 4 * 60 * 60


class Session(NamedTuple):
    session_id: str
    csrf_token: str
    created_at: float


class SessionStore:
    """A small bounded session table; oldest sessions are evicted first."""

    def __init__(self, *, max_sessions: int = MAX_SESSIONS, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions
        self._ttl_seconds = ttl_seconds

    def _prune(self, *, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.created_at > self._ttl_seconds
        ]
        for session_id in expired:
            del self._sessions[session_id]
        while len(self._sessions) >= self._max_sessions:
            oldest_id = min(self._sessions, key=lambda key: self._sessions[key].created_at)
            del self._sessions[oldest_id]

    def create(self) -> Session:
        now = time.time()
        self._prune(now=now)
        session = Session(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if session_id is None:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.created_at > self._ttl_seconds:
            del self._sessions[session_id]
            return None
        return session


def parse_session_cookie(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001 - malformed cookies are simply absent
        return None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel is not None else None


def session_cookie_header(session_id: str) -> str:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE_NAME] = session_id
    morsel = cookie[SESSION_COOKIE_NAME]
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    morsel["path"] = "/"
    # The development/production listener is loopback-only HTTP; requiring
    # "secure" here would prevent the cookie from ever being sent.
    return morsel.OutputString()


def default_allowed_host() -> str:
    return os.environ.get("VALHEIM_DASHBOARD_ALLOWED_HOST", "127.0.0.1:8080")


class RequestRejected(Exception):
    """A state-changing request failed a strict security check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def require_safe_state_change(
    *,
    method: str,
    host_header: str | None,
    origin_header: str | None,
    content_type_header: str | None,
    csrf_header: str | None,
    session: Session | None,
    allowed_host: str,
) -> None:
    """Reject anything but an exact-match same-origin authenticated JSON POST."""

    if method != "POST":
        raise RequestRejected("method_not_allowed", "Only POST is supported for this action")
    if host_header != allowed_host:
        raise RequestRejected("host_rejected", "Host header does not match the dashboard listener")
    expected_origin = f"http://{allowed_host}"
    if origin_header != expected_origin:
        raise RequestRejected("origin_rejected", "Origin header does not match the dashboard listener")
    if content_type_header != "application/json":
        raise RequestRejected("content_type_rejected", "Content-Type must be application/json")
    if session is None:
        raise RequestRejected("session_required", "A valid dashboard session is required")
    if not csrf_header or not secrets.compare_digest(csrf_header, session.csrf_token):
        raise RequestRejected("csrf_rejected", "CSRF token is missing or invalid")
