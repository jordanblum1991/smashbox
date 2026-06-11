"""Consolidated Data Health page — one page with the three data-quality
sections (Unmapped SKUs, Orphan Orders, Policy Violations)."""
from __future__ import annotations

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


def test_data_health_page_renders(client):
    r = client.get("/reports/recon-health")
    assert r.status_code == 200
    assert "Data Health" in r.text
    # All three sections present...
    assert "Unmapped SKUs" in r.text
    assert "Orphan Orders" in r.text
    assert "Policy Violations" in r.text
    # ...and all clear on an empty DB.
    assert "All clear" in r.text
