"""Consolidated /reports/recon-health page: Data Health + Recon as two tabs."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.models.tiktok_daily_metric import TikTokDailyMetric


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


def test_daily_explainer_reflects_resolved_bucketing(client: TestClient):
    """The daily-variance explainer must describe the CURRENT (resolved) model —
    settled days tie to $0.00, settled-day amber is a real break, recent-day amber
    is provisional Analytics lag — NOT the stale "expected 1-hour timezone noise"
    framing that contradicted the recon-break alerting."""
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="t.csv", stored_path="/tmp/t.csv")
        db.add(b)
        db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id="T1",
                  placed_at=datetime(2026, 5, 10, 12, 0), order_type=OrderType.PAID,
                  status="Shipped", brand="smashbox", gross_sales=Decimal("100.00"))
        db.add(o)
        db.flush()
        db.add(OrderLine(order_id=o.id, sku="SBX-001", quantity=1,
                         gross_sales=Decimal("100.00"), unit_cogs_snapshot=Decimal("0")))
        db.add(TikTokDailyMetric(import_batch_id=b.id, metric_date=date(2026, 5, 10),
                                 gmv=Decimal("100.00")))
        db.commit()

    body = client.get("/reports/recon-health?tab=recon&year=2026&month=5").text
    # New, accurate framing present:
    assert "Settled months tie to $0.00 per day" in body
    assert "is a real break" in body
    # Stale framing gone:
    assert "These small daily gaps are expected" not in body
    assert "7.80" not in body
    assert "informational, not as bugs" not in body


def test_legacy_data_health_redirects(client: TestClient):
    r = client.get("/reports/data-health", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reports/recon-health"


def test_legacy_reconciliation_redirects_preserving_month(client: TestClient):
    r = client.get("/reports/reconciliation?year=2026&month=5", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reports/recon-health?tab=recon&year=2026&month=5"
