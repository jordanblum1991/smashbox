"""Per-month ad-spend summary: one row per month with ad spend, showing gross
spend (GMV-Max only, excl. Shop Ads) and ROAS (Net Sales / GMV-Max spend).
Months with no GMV-Max ad spend are excluded; totals roll up gross and ROAS.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_spend import AdSpend
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
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


def test_gross_spend_is_gmv_max_only_excludes_shop_ads():
    # Order carries settlement Shop Ads; a GMV-Max AdSpend row also exists. The
    # Ad Spend page shows GMV-Max ONLY (matches Seller Center's Ad Cost), so
    # Shop Ads must NOT be added into gross_spend or the ROAS denominator here.
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Order(import_batch_id=b.id, tiktok_order_id="M",
                     placed_at=datetime(2026, 5, 15, 12, 0), order_type=OrderType.PAID,
                     status="Shipped", brand="smashbox",
                     gross_sales=Decimal("1000"), shop_ads_cost=Decimal("50")))
        _adspend(db, b.id, datetime(2026, 5, 15), 200)
        db.commit()
        result = compute_ad_spend_monthly(db)
    may = next(r for r in result.rows if r.month == 5)
    assert may.gross_spend == Decimal("200")        # GMV-Max only — NOT 250
    assert may.roas == Decimal("5")                 # 1000 / 200 (gmv-max), not 1000/250
    assert result.total_gross == Decimal("200")
    assert result.total_roas == Decimal("5")


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


def test_monthly_rows_include_campaign_kpis():
    with SessionLocal() as db:
        b = _batch(db)
        # May: GMV-Max spend + an entered campaign metric -> KPI columns filled.
        _order(db, b.id, "M", datetime(2026, 5, 15, 12, 0), 15769.65)
        _adspend(db, b.id, datetime(2026, 5, 15), "7824.02")
        db.add(GmvMaxCampaignMetric(year=2026, month=5,
                                    gross_revenue=Decimal("15769.65"), sku_orders=413))
        # Apr: spend but NO campaign metric -> KPI columns stay None.
        _order(db, b.id, "A", datetime(2026, 4, 15, 12, 0), 500)
        _adspend(db, b.id, datetime(2026, 4, 15), 100)
        db.commit()
        result = compute_ad_spend_monthly(db)

    may = next(r for r in result.rows if r.month == 5)
    apr = next(r for r in result.rows if r.month == 4)
    assert may.sku_orders == 413
    assert may.gross_revenue == Decimal("15769.65")
    assert may.cost_per_order == Decimal("18.94")     # 7824.02 / 413
    assert may.roi == Decimal("2.02")                 # 15769.65 / 7824.02
    assert apr.sku_orders is None and apr.cost_per_order is None
    assert apr.gross_revenue is None and apr.roi is None
    # Footer totals reuse the all-time campaign report.
    assert result.campaign_total is not None
    assert result.campaign_total.sku_orders == 413


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
