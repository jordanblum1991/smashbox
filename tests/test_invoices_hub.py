"""Consolidated /admin/invoices page: Vendor + Product invoices as two tabs.

Auth is disabled in tests (conftest pins SESSION_SECRET=""), so these admin
routes are reachable directly.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_invoices_hub_defaults_to_vendor_tab(client: TestClient):
    r = client.get("/admin/invoices")
    assert r.status_code == 200
    body = r.text
    assert 'href="/admin/invoices?tab=vendor"' in body
    assert 'href="/admin/invoices?tab=product"' in body
    # vendor body present (its "Create invoice" action + the empty/list section)
    assert "Create invoice" in body
    # Vendor tab is the active pill
    assert 'ring-1 ring-slate-200">Vendor' in body


def test_invoices_hub_product_tab(client: TestClient):
    r = client.get("/admin/invoices?tab=product")
    assert r.status_code == 200
    body = r.text
    assert "Smashbox Product Invoices" in body   # product body header
    assert "Open Balance" in body                # product summary tile
    assert 'ring-1 ring-slate-200">Product' in body
    # the vendor-only "Create invoice" button must not be on the product tab
    assert "Create invoice" not in body


def test_legacy_product_invoices_route_redirects(client: TestClient):
    r = client.get("/admin/product-invoices", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/invoices?tab=product")
