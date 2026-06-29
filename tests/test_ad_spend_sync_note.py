"""The Ad Spend & Campaign KPIs page shows when the GMV-Max API data last synced."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_ad_spend_shows_gmv_last_synced(client: TestClient):
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
            original_filename="api sync", stored_path="",
            completed_at=datetime(2026, 6, 25, 14, 0, 0),   # 14:00 UTC → same-day PT
        )
        db.add(b); db.flush()
        db.add(GmvMaxDailyMetric(import_batch_id=b.id, metric_date=date(2026, 6, 24),
                                 cost=Decimal("100"), sku_orders=5,
                                 gross_revenue=Decimal("500")))
        db.commit()

    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "synced" in r.text.lower()        # the label
    assert "Jun 25, 2026" in r.text          # the sync timestamp (shop-local date)


def test_ad_spend_handles_never_synced(client: TestClient):
    """With no GMV-Max data, the page still renders and notes it's not synced."""
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "synced" in r.text.lower()
