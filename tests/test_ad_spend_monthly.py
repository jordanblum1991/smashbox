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
from app.reports.ad_spend import (
    compute_ad_spend_daily,
    compute_ad_spend_fiscal,
    compute_ad_spend_monthly,
)


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


# --- Daily scope -----------------------------------------------------------

def test_daily_one_row_per_active_day_with_attributed_kpis():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 5, 1), "100.00", 5, "300.00")
        _daily(db, b.id, date(2026, 5, 2), "0.00", 0, "0.00")        # zero day → omitted
        _daily(db, b.id, date(2026, 5, 3), "50.00", 2, "120.00")
        db.commit()
        view = compute_ad_spend_daily(db, date(2026, 5, 1), date(2026, 5, 3))
    # Zero-activity day omitted; one row per active day, ascending.
    assert [r.day for r in view.rows] == [date(2026, 5, 1), date(2026, 5, 3)]
    d1 = view.rows[0]
    assert d1.gross_spend == Decimal("100.00")
    assert d1.sku_orders == 5
    assert d1.cost_per_order == Decimal("20.00")     # 100 / 5
    assert d1.gross_revenue == Decimal("300.00")
    assert d1.roi == Decimal("3.00")                 # 300 / 100
    # Window total ties to the shown rows.
    assert view.total.ad_cost == Decimal("150.00")
    assert view.total.sku_orders == 7
    assert view.total.gross_revenue == Decimal("420.00")


def test_fiscal_month_aggregates_29th_through_28th():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 4, 28), "50.00", 9, "200.00")    # prior fiscal month
        _daily(db, b.id, date(2026, 4, 29), "100.00", 5, "300.00")   # fiscal May opens
        _daily(db, b.id, date(2026, 5, 28), "50.00", 2, "120.00")    # fiscal May closes
        _daily(db, b.id, date(2026, 5, 29), "999.00", 99, "9999.00") # next fiscal month
        db.commit()
        view = compute_ad_spend_fiscal(db, 2026, 5, "month")
    assert len(view.rows) == 1                       # one combined fiscal-month row
    r = view.rows[0]
    assert r.year == 2026 and r.month == 5
    assert r.gross_spend == Decimal("150.00")        # Apr 29 + May 28 only
    assert r.sku_orders == 7
    assert r.gross_revenue == Decimal("420.00")
    assert view.total_gross == Decimal("150.00")


def test_fiscal_year_assigns_dec29_to_next_fiscal_year():
    with SessionLocal() as db:
        b = _batch(db)
        # Dec 29 2025 belongs to fiscal Jan 2026 (FY2026 opens Dec 29 2025).
        _daily(db, b.id, date(2025, 12, 29), "80.00", 4, "200.00")
        _daily(db, b.id, date(2026, 11, 29), "60.00", 3, "150.00")   # fiscal Dec 2026
        _daily(db, b.id, date(2025, 12, 28), "777.00", 7, "7777.00") # FY2025, excluded
        db.commit()
        view = compute_ad_spend_fiscal(db, 2026, 12, "year")
    months = {r.month for r in view.rows}
    assert 1 in months and 12 in months              # fiscal Jan + fiscal Dec have data
    assert view.total_gross == Decimal("140.00")     # 80 + 60; the Dec 28 2025 row excluded


def test_daily_window_is_inclusive_of_end_day():
    with SessionLocal() as db:
        b = _batch(db)
        _daily(db, b.id, date(2026, 5, 1), "10.00", 1, "30.00")
        _daily(db, b.id, date(2026, 5, 3), "20.00", 2, "60.00")
        _daily(db, b.id, date(2026, 5, 4), "40.00", 4, "120.00")   # outside [1,3]
        db.commit()
        view = compute_ad_spend_daily(db, date(2026, 5, 1), date(2026, 5, 3))
    assert [r.day for r in view.rows] == [date(2026, 5, 1), date(2026, 5, 3)]
    assert view.total.ad_cost == Decimal("30.00")    # May 4 excluded
