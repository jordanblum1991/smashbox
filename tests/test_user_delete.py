"""Admin hard-delete of users — removal, last-admin guard, FK cleanup.

Runs auth-OFF (conftest), so require_admin is permissive and request.state.user
is None (the self-delete guard is exercised separately under real auth elsewhere).
"""
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.sku_alias import SkuAlias
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _user(db, email, role=UserRole.MEMBER, active=True):
    u = User(email=email, name="N", password_hash=hash_password("x" * 8), role=role, is_active=active)
    db.add(u)
    db.flush()
    return u


def test_delete_removes_user(client):
    with SessionLocal() as db:
        _user(db, "admin@x.com", role=UserRole.ADMIN)  # keep an admin around
        mid = _user(db, "member@x.com").id
        db.commit()
    r = client.post(f"/admin/users/{mid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(User, mid) is None


def test_cannot_delete_last_active_admin(client):
    with SessionLocal() as db:
        aid = _user(db, "only-admin@x.com", role=UserRole.ADMIN).id
        db.commit()
    client.post(f"/admin/users/{aid}/delete", follow_redirects=False)
    with SessionLocal() as db:
        assert db.get(User, aid) is not None  # blocked — still there


def test_delete_nulls_sku_alias_fk(client):
    with SessionLocal() as db:
        _user(db, "admin@x.com", role=UserRole.ADMIN)  # so target isn't last admin
        m = _user(db, "author@x.com")
        db.add(SkuAlias(alias_sku="C1", canonical_sku="SBX-C1", created_by_user_id=m.id))
        db.commit()
        mid = m.id
    r = client.post(f"/admin/users/{mid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(User, mid) is None
        alias = db.query(SkuAlias).one()
        assert alias.created_by_user_id is None  # FK detached, alias preserved
