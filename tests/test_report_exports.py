"""CSV download endpoints for the Ad Spend, Reconciliation, and Data Health pages."""
import csv
import io

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


def _rows(r):
    return list(csv.reader(io.StringIO(r.text)))


def test_ad_spend_csv(client: TestClient):
    r = client.get("/reports/ad-spend.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert _rows(r)[0] == [
        "Year", "Month", "Gross Spend (GMV-Max)", "ROAS", "SKU Orders",
        "Cost per Order", "Gross Revenue", "ROI",
    ]


def test_reconciliation_csv(client: TestClient):
    r = client.get("/reports/reconciliation.csv?year=2026&month=5")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert _rows(r)[0] == [
        "Date", "Our GMV", "TikTok GMV", "Variance", "Refunds",
        "Net Customer Sales", "Orders",
    ]


def test_data_health_csv(client: TestClient):
    r = client.get("/reports/data-health.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert _rows(r)[0] == ["Issue Type", "Identifier", "Detail", "Amount", "Date"]
