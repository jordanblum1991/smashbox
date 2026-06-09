"""GMV Max campaign KPI report — derives SKU Orders, Cost per Order, Gross
Revenue, ROI from the manually-entered `GmvMaxCampaignMetric` rows plus the
imported GMV-Max `AdSpend`. These are campaign-attributed (TikTok-reported)
figures; the test pins them to the real Seller Center May-2026 numbers.

  Cost per Order = Ad Cost ÷ SKU Orders     (denominator is SKU orders, verified)
  ROI            = Gross Revenue ÷ Ad Cost   (Seller Center's displayed multiple)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.ad_spend import AdSpend
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.models.import_batch import ImportBatch, ImportFileKind
from app.reports.gmv_max_campaign_kpis import compute_gmv_max_campaign_kpis


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _metric(db, year, month, gross_revenue, sku_orders):
    db.add(GmvMaxCampaignMetric(
        year=year, month=month,
        gross_revenue=Decimal(str(gross_revenue)), sku_orders=sku_orders,
    ))
    db.flush()


def _spend(db, batch_id, dt, amount, campaign_id="c1"):
    db.add(AdSpend(
        import_batch_id=batch_id, spend_date=dt,
        campaign_id=campaign_id, amount=Decimal(str(amount)),
    ))
    db.flush()


def _batch(db):
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ADS,
        original_filename="x.xlsx", stored_path="/tmp/x.xlsx",
    )
    db.add(b); db.flush()
    return b.id


def test_single_month_matches_seller_center_may():
    with SessionLocal() as db:
        bid = _batch(db)
        _metric(db, 2026, 5, "15769.65", 413)
        _spend(db, bid, datetime(2026, 5, 10), "7824.02")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        assert k.has_data is True
        assert k.gross_revenue == Decimal("15769.65")
        assert k.sku_orders == 413
        assert k.ad_cost == Decimal("7824.02")
        assert k.cost_per_order == Decimal("18.94")   # 7824.02 / 413
        assert k.roi == Decimal("2.02")               # 15769.65 / 7824.02


def test_all_time_sums_months():
    with SessionLocal() as db:
        bid = _batch(db)
        _metric(db, 2026, 4, "15168.53", 423)
        _metric(db, 2026, 5, "15769.65", 413)
        _spend(db, bid, datetime(2026, 4, 10), "13596.28")
        _spend(db, bid, datetime(2026, 5, 10), "7824.02")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db)  # all-time
        assert k.gross_revenue == Decimal("30938.18")
        assert k.sku_orders == 836
        assert k.ad_cost == Decimal("21420.30")
        # Σ ad ÷ Σ sku, Σ gr ÷ Σ ad
        assert k.cost_per_order == Decimal("25.62")   # 21420.30 / 836
        assert k.roi == Decimal("1.44")               # 30938.18 / 21420.30


def test_no_data_zeros_no_divide():
    with SessionLocal() as db:
        k = compute_gmv_max_campaign_kpis(db)
        assert k.has_data is False
        assert k.gross_revenue == Decimal("0")
        assert k.sku_orders == 0
        assert k.ad_cost == Decimal("0")
        assert k.cost_per_order == Decimal("0")
        assert k.roi == Decimal("0")


def test_zero_sku_orders_no_divide():
    with SessionLocal() as db:
        bid = _batch(db)
        _metric(db, 2026, 5, "100.00", 0)
        _spend(db, bid, datetime(2026, 5, 10), "50.00")
        db.commit()
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        assert k.cost_per_order == Decimal("0")
        assert k.roi == Decimal("2.00")


def test_zero_ad_cost_no_divide():
    with SessionLocal() as db:
        _metric(db, 2026, 5, "100.00", 10)
        db.commit()
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        assert k.has_data is True
        assert k.ad_cost == Decimal("0")
        assert k.roi == Decimal("0")
        assert k.cost_per_order == Decimal("0")


def test_ad_cost_excludes_months_without_metric():
    # June has spend but no entered metric → it must NOT count toward Ad Cost
    # (else all-time Cost/Order and ROI mix June's spend with no June revenue).
    with SessionLocal() as db:
        bid = _batch(db)
        _metric(db, 2026, 5, "15769.65", 413)
        _spend(db, bid, datetime(2026, 5, 10), "7824.02")
        _spend(db, bid, datetime(2026, 6, 10), "1454.11")   # spend, no May-vs metric
        db.commit()
        k = compute_gmv_max_campaign_kpis(db)  # all-time
        assert k.ad_cost == Decimal("7824.02")   # June's 1454.11 excluded
        assert k.cost_per_order == Decimal("18.94")
        assert k.roi == Decimal("2.02")


def test_window_excludes_other_months():
    with SessionLocal() as db:
        bid = _batch(db)
        _metric(db, 2026, 4, "15168.53", 423)
        _metric(db, 2026, 6, "900.00", 30)
        _spend(db, bid, datetime(2026, 4, 10), "13596.28")
        _spend(db, bid, datetime(2026, 6, 10), "1454.11")
        db.commit()
        # Window = May only → no metrics, no spend.
        k = compute_gmv_max_campaign_kpis(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        assert k.has_data is False
        assert k.gross_revenue == Decimal("0")
        assert k.ad_cost == Decimal("0")
