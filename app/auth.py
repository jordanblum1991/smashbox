"""Per-user authentication (Phase 1 of the multi-user roadmap).

Replaces the v1 HTTP Basic credential with email+password logins backed by:
- bcrypt-hashed passwords (passlib)
- Server-side signed-cookie sessions (Starlette SessionMiddleware, signed
  with `settings.session_secret`)
- A small middleware that loads `request.state.user` from the session and
  redirects unauthenticated requests to /login

Forward-compat hooks:
- `request.state.user.role` is exposed so future routes can demand admin
- The empty-secret escape hatch (settings.session_secret == "") keeps local
  dev frictionless — same UX as the old empty-BASIC_AUTH_PASSWORD pattern

Phase 2 will add a `shop_id` FK on User + every transactional table, and the
middleware will start scoping queries by `request.state.user.shop_id`.
"""
import bcrypt
from fastapi import HTTPException, Request as FastAPIRequest
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import settings
from app.db import SessionLocal
from app.models.user import User, UserRole

# bcrypt directly — passlib's wrapper has been broken since bcrypt 4.0
# (passlib reads `bcrypt.__about__` which 4.x removed). Direct API is
# small enough to use without an abstraction.


def hash_password(plaintext: str) -> str:
    """Return the bcrypt hash of `plaintext` (~12 cost factor by default,
    ~250ms verify on modern hardware)."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time bcrypt verify. Returns False on any malformed-hash
    error rather than raising — caller is doing user-facing login, so a
    benign no-match is the right behaviour."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---- Route dependencies ----------------------------------------------------

def require_admin(request: FastAPIRequest) -> User:
    """Route dependency: ensure the request is authenticated and the user is
    an admin. SessionAuthMiddleware has already attached the user to
    request.state; if auth is disabled (settings.session_secret == ""), this
    is a no-op so local dev keeps working.

    Returns the User so the route handler can use it without re-fetching.
    """
    if not settings.session_secret:
        # Dev mode — auth disabled, treat as admin
        return None  # type: ignore[return-value]
    user = getattr(request.state, "user", None)
    if user is None or user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Gate every route on a session-attached User.

    Behaviour:
      - `settings.session_secret == ""`  → auth disabled (local dev)
      - `/static/*`, `/login`, `/logout` → always reachable
      - Otherwise: look up session["user_id"]; load the User; attach to
        `request.state.user`. Missing/invalid → redirect to /login.
    """

    EXEMPT_PREFIXES = ("/static/", "/login", "/logout", "/healthz")

    async def dispatch(self, request: Request, call_next):
        if not settings.session_secret:
            # Dev mode — pretend there's no auth at all
            request.state.user = None
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
            request.state.user = None
            return await call_next(request)

        user_id = request.session.get("user_id") if hasattr(request, "session") else None
        if user_id is None:
            return RedirectResponse(url=f"/login?next={path}", status_code=303)

        with SessionLocal() as db:
            user = db.execute(
                select(User).where(User.id == user_id).where(User.is_active.is_(True))
            ).scalar_one_or_none()
        if user is None:
            # Session is stale (user deleted or deactivated) → bounce to login
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        request.state.user = user
        return await call_next(request)


# ---- Legacy HTTP Basic — kept for old deploys that haven't yet set ---------
# `SESSION_SECRET`. Once that secret is set on production, the SessionAuthMiddleware
# is the active gate and BasicAuthMiddleware can be removed in a follow-up.

import secrets


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Pre-Phase-1 single-credential gate. Inert when basic_auth_password is
    empty. Slated for removal once all deploys are on SessionAuthMiddleware."""

    EXEMPT_PREFIXES = ("/static/", "/healthz")

    async def dispatch(self, request: Request, call_next):
        if not settings.basic_auth_password:
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic ") and _basic_matches(header):
            return await call_next(request)
        return Response(
            status_code=401,
            content="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="Smashbox", charset="UTF-8"'},
        )


def _basic_matches(auth_header: str) -> bool:
    import base64
    try:
        raw = base64.b64decode(auth_header[len("Basic "):], validate=True).decode("utf-8")
        username, _, password = raw.partition(":")
    except Exception:  # noqa: BLE001
        return False
    return (
        secrets.compare_digest(username, settings.basic_auth_username)
        and secrets.compare_digest(password, settings.basic_auth_password)
    )
