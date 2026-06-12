"""Google sign-in — the email→User resolver (no self-registration) + gating."""
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.user import User, UserRole
from app.routers.auth import resolve_google_user


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(User(email="user@example.com", name="U", password_hash=hash_password("x" * 8),
                    role=UserRole.ADMIN, is_active=True))
        db.add(User(email="off@example.com", name="Off", password_hash=hash_password("x" * 8),
                    role=UserRole.MEMBER, is_active=False))
        db.commit()
    yield


def test_existing_active_user_resolves():
    with SessionLocal() as db:
        # Case-insensitive match against the stored lower-case email.
        user, err = resolve_google_user(db, {"email": "User@Example.com", "email_verified": True})
        assert err is None and user is not None and user.email == "user@example.com"


def test_unknown_email_rejected():
    with SessionLocal() as db:
        user, err = resolve_google_user(db, {"email": "stranger@evil.com", "email_verified": True})
        assert user is None and "No Smashbox account" in err


def test_inactive_user_rejected():
    with SessionLocal() as db:
        user, err = resolve_google_user(db, {"email": "off@example.com", "email_verified": True})
        assert user is None and err


def test_unverified_email_rejected():
    with SessionLocal() as db:
        user, err = resolve_google_user(db, {"email": "user@example.com", "email_verified": False})
        assert user is None and "isn't verified" in err


def test_missing_email_rejected():
    with SessionLocal() as db:
        user, err = resolve_google_user(db, {})
        assert user is None and err


def test_button_hidden_when_unconfigured():
    client = TestClient(app)
    assert "Sign in with Google" not in client.get("/login").text


def test_button_shows_and_login_redirects_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "secret")
    client = TestClient(app)
    assert "Sign in with Google" in client.get("/login").text


def test_google_login_route_errors_when_unconfigured():
    client = TestClient(app)
    r = client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
