"""Consolidated /reports/recon-health page: Data Health + Recon as two tabs."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_recon_health_defaults_to_data_health(client: TestClient):
    # No ?tab → Data Health section (the default).
    r = client.get("/reports/recon-health")
    assert r.status_code == 200
    body = r.text
    assert "Unmapped SKUs" in body
    assert "Orphan Orders" in body
    assert "Policy Violations" in body


def test_recon_health_recon_section(client: TestClient):
    r = client.get("/reports/recon-health?tab=recon")
    assert r.status_code == 200
    body = r.text
    # data-health-only content must not be on the reconciliation view
    assert "Unmapped SKUs" not in body


def test_legacy_data_health_redirects(client: TestClient):
    r = client.get("/reports/data-health", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reports/recon-health"


def test_legacy_reconciliation_redirects_preserving_month(client: TestClient):
    r = client.get("/reports/reconciliation?year=2026&month=5", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reports/recon-health?tab=recon&year=2026&month=5"
