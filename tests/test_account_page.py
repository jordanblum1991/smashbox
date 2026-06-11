"""Consolidated /account page: self-service password change + (admin) user mgmt.

Auth is disabled in tests (conftest pins SESSION_SECRET=""), so SessionAuth is
not installed and require_admin is a no-op — the admin section therefore renders
and gated routes are reachable directly, mirroring the other admin-route tests
in this suite. We don't exercise the password POST here because it reads
request.session, which only exists when SessionMiddleware is installed (i.e.
when auth is on).
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_account_page_has_password_and_user_management(client: TestClient):
    r = client.get("/account")
    assert r.status_code == 200
    body = r.text
    # password section (every user)
    assert "Change password" in body
    assert 'name="current_password"' in body
    # admin user-management section (shown because auth is disabled in tests)
    assert "Manage users" in body
    assert "Add new user" in body
    assert "All users" in body


def test_legacy_password_route_redirects_to_account(client: TestClient):
    r = client.get("/account/password", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account"


def test_legacy_admin_users_route_redirects_to_account(client: TestClient):
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account"


def test_created_user_renders_on_account_page(client: TestClient):
    # The admin create-user POST is unchanged; it should redirect back to
    # /account, and the new user should appear in the All-users list there.
    r = client.post(
        "/admin/users",
        data={
            "email": "consolidation-test@example.com",
            "name": "Consolidation Test",
            "password": "supersecret123",
            "role": "member",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/account")

    page = client.get("/account")
    assert "consolidation-test@example.com" in page.text
