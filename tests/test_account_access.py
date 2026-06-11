"""Access-control for the consolidated /account page, under REAL auth.

The rest of the suite runs auth-OFF (conftest pins SESSION_SECRET=""), which
makes `show_admin` always true — so it can't prove the core requirement that a
*member* is denied the user-management section. This test stands up a small app
with the session + auth middleware (mirroring app.main) and a real login, then
asserts the gate from both sides.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.auth import SessionAuthMiddleware, hash_password
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.user import User, UserRole
from app.routers import auth as auth_router

_SECRET = "test-secret-account-access"


def _build_authed_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router.router)
    # Same order as app.main: SessionAuth inner, Session outer, so
    # request.session is available to the auth middleware.
    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=_SECRET)
    return app


@pytest.fixture
def authed(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "session_secret", _SECRET)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.query(User).delete()
        db.add(User(email="admin@example.com", name="Adminy",
                    password_hash=hash_password("password123"),
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(email="member@example.com", name="Memberly",
                    password_hash=hash_password("password123"),
                    role=UserRole.MEMBER, is_active=True))
        db.commit()
    return TestClient(_build_authed_app())


def _login(client: TestClient, email: str) -> None:
    r = client.post("/login", data={"email": email, "password": "password123"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text


def test_member_can_change_password_but_not_manage_users(authed: TestClient):
    _login(authed, "member@example.com")
    r = authed.get("/account")
    assert r.status_code == 200
    body = r.text
    assert "Change password" in body
    assert 'name="current_password"' in body
    # The admin-only section must be entirely absent for a member.
    assert "Manage users" not in body
    assert "Add new user" not in body
    assert 'action="/admin/users"' not in body


def test_admin_sees_user_management(authed: TestClient):
    _login(authed, "admin@example.com")
    r = authed.get("/account")
    assert r.status_code == 200
    body = r.text
    assert "Change password" in body
    assert "Manage users" in body
    assert "Add new user" in body


def test_account_requires_login(authed: TestClient):
    # No session → auth middleware bounces to /login.
    r = authed.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
