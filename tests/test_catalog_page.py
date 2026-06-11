"""Consolidated /admin/catalog page: SKUs + Bundles as two server-rendered tabs.

Auth is disabled in tests (conftest pins SESSION_SECRET=""), so require_admin is
a no-op and these admin routes are reachable directly.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_catalog_defaults_to_skus_tab(client: TestClient):
    r = client.get("/admin/catalog")
    assert r.status_code == 200
    body = r.text
    # tab bar present with both tabs
    assert 'href="/admin/catalog?tab=skus"' in body
    assert 'href="/admin/catalog?tab=bundles"' in body
    # SKUs panel rendered (its ag-grid asset + add-SKU form)
    assert "ag-grid-community.min.js" in body
    assert 'action="/admin/skus"' in body
    assert "SKU_ROWS" in body


def test_catalog_bundles_tab_renders_bundles_panel(client: TestClient):
    r = client.get("/admin/catalog?tab=bundles")
    assert r.status_code == 200
    body = r.text
    assert 'action="/admin/bundles"' in body
    assert "BUNDLE_ROWS" in body
    # the SKUs-only add form should NOT be on the bundles tab
    assert 'action="/admin/skus"' not in body


def test_legacy_skus_route_redirects_to_catalog(client: TestClient):
    r = client.get("/admin/skus", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/catalog?tab=skus")


def test_legacy_bundles_route_redirects_to_catalog(client: TestClient):
    r = client.get("/admin/bundles", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/catalog?tab=bundles")
