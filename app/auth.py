"""HTTP Basic Auth — single shared credential gating the whole app.

This is the deliberate v1 of authentication: one username, one password, set
via environment variables (`BASIC_AUTH_USERNAME`, `BASIC_AUTH_PASSWORD`).
Designed to be the cheapest possible gate when exposing the dashboard on the
public internet so the financial data isn't sitting unauthenticated.

When this grows up to per-user logins + RBAC, swap the middleware out — the
public surface (route protection) stays the same.

Behaviour:
- If `basic_auth_password` is empty, auth is disabled (local dev convenience).
- `/static/*` is always reachable so the browser can load CSS/JS without
  prompting again on every asset request.
- Any other route returns 401 with `WWW-Authenticate: Basic` until valid
  credentials arrive — browser shows its native login dialog.
"""
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Reject every request that doesn't carry a valid Basic Auth header."""

    EXEMPT_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        # Empty password = auth disabled (local dev).
        if not settings.basic_auth_password:
            return await call_next(request)

        if any(request.url.path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        if header.startswith("Basic ") and _matches(header):
            return await call_next(request)

        return Response(
            status_code=401,
            content="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="Smashbox", charset="UTF-8"'},
        )


def _matches(auth_header: str) -> bool:
    """Constant-time compare against the configured credential pair.

    Uses secrets.compare_digest to avoid leaking timing information about
    which character mismatched, which is the standard practice for any
    cred check even when the threat model is low.
    """
    import base64
    try:
        raw = base64.b64decode(auth_header[len("Basic "):], validate=True).decode("utf-8")
        username, _, password = raw.partition(":")
    except Exception:  # noqa: BLE001 — malformed header → just reject
        return False
    ok_user = secrets.compare_digest(username, settings.basic_auth_username)
    ok_pass = secrets.compare_digest(password, settings.basic_auth_password)
    return ok_user and ok_pass
