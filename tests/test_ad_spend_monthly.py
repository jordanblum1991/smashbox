"""Per-month ad-spend summary, sourced from the daily GMV Max campaign metrics
(GmvMaxDailyMetric). One row per month with campaign activity, clamped to any
date window; spend = the report's Cost, ROAS = Net Customer Sales ÷ Cost;
totals aggregate the shown rows. Day-accurate windows.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.order import Order, OrderType
from app.reports.ad_spend import compute_ad_spend_monthly


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _daily(db, bid, d: date, cost, sku, gr):
    db.add(GmvMaxDailyMetric(import_batch_id=bid, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=sku, gross_revenue=Decimal(str(gr))))
    db.flush()


def _order(db, bid, oid, placed, gross):
    db.add(Order(import_batch_id=bid, tiktok_order_id=oid, placed_at=placed,
                 order_type=OrderType.PAID, status="Shipped", brand="smashbox",
                 gross_sales=Decimal(str(gross))))
    db.flush()


def test_rows_sourced_from_daily_metrics():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 5, 1), "100.00", 5, "300.00")
        _daily(db, b.id, date(2026, 5, 2), "100.00", 5, "300.00")   # May: cost 200, sku 10, gr 600
        _order(db, b.id, "M", datetime(2026, 5, 1, 12, 0), 1000)    # within metric days → net 1000, ROAS 5
        db.commit()
        result = compute_ad_spend_monthly(db)
    may = next(r for r in result.rows if r.month == 5)
    assert may.gross_spend == Decimal("200.00")     # Total Gross Spend = campaign Cost
    assert may.sku_orders == 10
    assert may.cost_per_order == Decimal("20.00")   # 200 / 10
    assert may.gross_revenue == Decimal("600.00")
    assert may.roi == Decimal("3.00")               # 600 / 200
    assert may.roas == Decimal("5")                 # net 1000 / cost 200
    assert result.total_gross == Decimal("200.00")
    assert result.campaign_total.sku_orders == 10
    assert result.campaign_total.ad_cost == Decimal("200.00")


def test_excludes_zero_activity_months():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 1, 1), "0.00", 0, "0.00")        # zero day → excluded
        _daily(db, b.id, date(2026, 5, 1), "100.00", 5, "300.00")
        db.commit()
        result = compute_ad_spend_monthly(db)
    assert [(r.year, r.month) for r in result.rows] == [(2026, 5)]


def test_window_is_day_accurate():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 5, 1), "10.00", 1, "30.00")
        _daily(db, b.id, date(2026, 5, 10), "20.00", 2, "60.00")
        _daily(db, b.id, date(2026, 5, 20), "40.00", 4, "120.00")
        db.commit()
        # [May 1, May 11) → May 1 + May 10 only (exclusive end).
        result = compute_ad_spend_monthly(db, datetime(2026, 5, 1), datetime(2026, 5, 11))
    may = next(r for r in result.rows if r.month == 5)
    assert may.gross_spend == Decimal("30.00")
    assert may.sku_orders == 3
    assert may.gross_revenue == Decimal("90.00")
    assert result.total_gross == Decimal("30.00")


def test_empty_when_no_daily_metrics():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, "O", datetime(2026, 3, 15, 12, 0), 300)    # orders but no campaign data
        db.commit()
        result = compute_ad_spend_monthly(db)
    assert result.rows == []
    assert result.total_gross == Decimal("0")
    assert result.total_roas == Decimal("0")
