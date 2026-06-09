"""GMV Max campaign KPI aggregation from daily metrics (GmvMaxDailyMetric).

  Cost per Order = Ad Cost ÷ SKU Orders
  ROI            = Gross Revenue ÷ Ad Cost
Windows are [start, end) with an EXCLUSIVE end, day-accurate.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.reports.gmv_max_campaign_kpis import compute_gmv_max_campaign_kpis


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="c.xlsx", stored_path="c.xlsx")
    db.add(b); db.flush()
    return b.id


def _day(db, bid, d: date, cost, sku, gr):
    db.add(GmvMaxDailyMetric(import_batch_id=bid, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=sku, gross_revenue=Decimal(str(gr))))
    db.flush()


def test_window_aggregates_days():
    with SessionLocal() as db:
        bid = _batch(db)
        _day(db, bid, date(2026, 5, 1), "100.00", 5, "300.00")
        _day(db, bid, date(2026, 5, 2), "50.00", 2, "120.00")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        assert k.has_data is True
        assert k.gross_revenue == Decimal("420.00")
        assert k.sku_orders == 7
        assert k.ad_cost == Decimal("150.00")
        assert k.cost_per_order == Decimal("21.43")   # 150 / 7
        assert k.roi == Decimal("2.80")               # 420 / 150


def test_all_time_when_no_window():
    with SessionLocal() as db:
        bid = _batch(db)
        _day(db, bid, date(2026, 4, 10), "40.00", 4, "160.00")
        _day(db, bid, date(2026, 5, 10), "60.00", 6, "240.00")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db)
        assert k.ad_cost == Decimal("100.00")
        assert k.sku_orders == 10
        assert k.gross_revenue == Decimal("400.00")


def test_day_accurate_range_excludes_outside():
    with SessionLocal() as db:
        bid = _batch(db)
        _day(db, bid, date(2026, 5, 1), "10.00", 1, "30.00")
        _day(db, bid, date(2026, 5, 10), "20.00", 2, "60.00")
        _day(db, bid, date(2026, 5, 20), "40.00", 4, "120.00")
        db.commit()
        # [May 1, May 11) → May 1 and May 10 only (exclusive end).
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 5, 11))
        assert k.ad_cost == Decimal("30.00")
        assert k.sku_orders == 3
        assert k.gross_revenue == Decimal("90.00")


def test_zero_activity_has_data_false():
    with SessionLocal() as db:
        bid = _batch(db)
        _day(db, bid, date(2026, 1, 1), "0.00", 0, "0.00")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db)
        assert k.has_data is False
        assert k.cost_per_order == Decimal("0")
        assert k.roi == Decimal("0")


def test_empty_no_divide():
    with SessionLocal() as db:
        k = compute_gmv_max_campaign_kpis(db)
        assert k.has_data is False
        assert k.ad_cost == Decimal("0")
        assert k.roi == Decimal("0")
        assert k.cost_per_order == Decimal("0")
