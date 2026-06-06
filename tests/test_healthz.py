"""Liveness health check for Fly.

`/healthz` is a DB-free liveness probe: it answers "is the event loop
responsive?" and nothing more. It must (a) exist and return 200, and (b) be
exempt from auth — otherwise Fly's probe gets a 303 redirect to /login, counts
the check as failing, and restarts a perfectly healthy app into a boot loop.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.auth import SessionAuthMiddleware
from app.config import settings
from app.main import app


def test_healthz_returns_ok():
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text.strip() == "ok"


def test_healthz_exempt_from_session_auth(monkeypatch):
    """With auth ON, /healthz returns 200 (probe passes) while a protected
    path redirects to /login."""
    monkeypatch.setattr(settings, "session_secret", "test-secret")

    async def health(request):
        return PlainTextResponse("ok")

    async def private(request):
        return PlainTextResponse("secret")

    test_app = Starlette(routes=[
        Route("/healthz", health),
        Route("/private", private),
    ])
    # Mirror main.py's middleware order: SessionAuth (inner) then Session (outer)
    # so request.session is available to the auth middleware.
    test_app.add_middleware(SessionAuthMiddleware)
    test_app.add_middleware(SessionMiddleware, secret_key="test-secret")
    client = TestClient(test_app)

    assert client.get("/healthz").status_code == 200

    r = client.get("/private", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
