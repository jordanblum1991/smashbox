"""Per-month ad-spend summary: one row per month with ad spend, showing gross
spend (GMV-Max + Shop Ads) and ROAS (Net Sales / gross spend). Months with no
ad spend are excluded; totals roll up gross and overall ROAS.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_spend import AdSpend
from app.models.order import Order, OrderType
from app.reports.ad_spend import compute_ad_spend_monthly


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _order(db, bid, oid, placed, gross):
    db.add(Order(import_batch_id=bid, tiktok_order_id=oid, placed_at=placed,
                 order_type=OrderType.PAID, status="Shipped", brand="smashbox",
                 gross_sales=Decimal(str(gross))))
    db.flush()


def _adspend(db, bid, spend_date, amount):
    db.add(AdSpend(import_batch_id=bid, spend_date=spend_date, campaign_id="C1",
                   amount=Decimal(str(amount))))
    db.flush()


def test_monthly_rows_exclude_months_without_ad_spend():
    with SessionLocal() as db:
        b = _batch(db)
        # Apr: spend 100, sales 500 -> roas 5
        _order(db, b.id, "A", datetime(2026, 4, 15, 12, 0), 500)
        _adspend(db, b.id, datetime(2026, 4, 15), 100)
        # May: spend 200, sales 1000 -> roas 5
        _order(db, b.id, "M", datetime(2026, 5, 15, 12, 0), 1000)
        _adspend(db, b.id, datetime(2026, 5, 15), 200)
        # Mar: sales but NO ad spend -> excluded
        _order(db, b.id, "R", datetime(2026, 3, 15, 12, 0), 300)
        db.commit()
        result = compute_ad_spend_monthly(db)

    months = [(r.year, r.month) for r in result.rows]
    assert (2026, 3) not in months                 # no ad spend -> excluded
    assert months == [(2026, 4), (2026, 5)]         # chronological, ad-spend months only

    apr = next(r for r in result.rows if r.month == 4)
    may = next(r for r in result.rows if r.month == 5)
    assert apr.gross_spend == Decimal("100")
    assert apr.roas == Decimal("5")
    assert may.gross_spend == Decimal("200")
    assert may.roas == Decimal("5")


def test_monthly_totals():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, "A", datetime(2026, 4, 15, 12, 0), 500)
        _adspend(db, b.id, datetime(2026, 4, 15), 100)
        _order(db, b.id, "M", datetime(2026, 5, 15, 12, 0), 1000)
        _adspend(db, b.id, datetime(2026, 5, 15), 200)
        db.commit()
        result = compute_ad_spend_monthly(db)
    assert result.total_gross == Decimal("300")            # 100 + 200
    assert result.total_roas == Decimal("5")               # (500+1000) / (100+200)


def test_monthly_empty_when_no_ad_spend():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, "R", datetime(2026, 3, 15, 12, 0), 300)
        db.commit()
        result = compute_ad_spend_monthly(db)
    assert result.rows == []
    assert result.total_gross == Decimal("0")
    assert result.total_roas == Decimal("0")
