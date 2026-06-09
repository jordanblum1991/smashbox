"""Admin CRUD for GMV Max campaign metrics — upsert (edit-not-stack),
validation (303 flash, no write), and delete. Calls the route functions
directly, the same pattern as test_sku_bulk_edit.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.db import Base, SessionLocal, engine
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.routers.gmv_max_campaign_metrics import (
    delete_gmv_max_campaign_metric,
    upsert_gmv_max_campaign_metric,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _rows(db):
    return db.query(GmvMaxCampaignMetric).all()


def test_upsert_creates_then_overwrites():
    with SessionLocal() as db:
        r = upsert_gmv_max_campaign_metric(
            year="2026", month="5", gross_revenue="15769.65", sku_orders="413",
            note="from report", db=db,
        )
        assert r.status_code == 303
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0].gross_revenue == Decimal("15769.65")
        assert rows[0].sku_orders == 413

        # Re-save same month → overwrite in place, still one row.
        upsert_gmv_max_campaign_metric(
            year="2026", month="5", gross_revenue="16000.00", sku_orders="420",
            note=None, db=db,
        )
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0].gross_revenue == Decimal("16000.00")
        assert rows[0].sku_orders == 420
        assert rows[0].note is None


def test_invalid_month_rejected_no_write():
    with SessionLocal() as db:
        r = upsert_gmv_max_campaign_metric(
            year="2026", month="13", gross_revenue="100", sku_orders="5", note=None, db=db,
        )
        assert r.status_code == 303
        assert "error=" in str(r.headers.get("location"))
        assert _rows(db) == []


def test_negative_gross_revenue_rejected():
    with SessionLocal() as db:
        r = upsert_gmv_max_campaign_metric(
            year="2026", month="5", gross_revenue="-1", sku_orders="5", note=None, db=db,
        )
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert _rows(db) == []


def test_non_numeric_sku_orders_rejected():
    with SessionLocal() as db:
        r = upsert_gmv_max_campaign_metric(
            year="2026", month="5", gross_revenue="100", sku_orders="4.5", note=None, db=db,
        )
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert _rows(db) == []


def test_delete_removes_row():
    with SessionLocal() as db:
        upsert_gmv_max_campaign_metric(
            year="2026", month="5", gross_revenue="100", sku_orders="5", note=None, db=db,
        )
        row_id = _rows(db)[0].id
        r = delete_gmv_max_campaign_metric(row_id=row_id, db=db)
        assert r.status_code == 303
        assert _rows(db) == []


def test_delete_missing_404():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            delete_gmv_max_campaign_metric(row_id=999999, db=db)
        assert ei.value.status_code == 404
