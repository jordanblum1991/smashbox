"""Ad Spend Summary page shows only Total Gross Spend + ROAS for the period.

No ad-credit info, no monthly table, no Reimbursements link. (Credit entry still
lives on the Reimbursements page, reachable from the nav.)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_ad_spend_page_shows_gross_and_roas(client):
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "Total Gross Spend" in r.text
    assert "ROAS" in r.text


def test_ad_spend_page_has_no_credit_info_or_reimbursements_link(client):
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    # credit tiles / columns removed
    assert "Total Ad Credits Applied" not in r.text
    assert "Net of Credits" not in r.text
    assert "Ad Credits Applied" not in r.text
    # monthly detail table removed
    assert "Monthly detail" not in r.text
    # the in-page "Reimbursements →" header link removed (nav link text differs)
    assert "Reimbursements →" not in r.text
