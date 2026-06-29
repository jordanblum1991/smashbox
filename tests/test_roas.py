"""ROAS is measured on GROSS ad spend — it must NOT net out ad credits.

ROAS answers "revenue per $1 of advertising," so the denominator is total
(gross) ad spend. Ad credits are a separate reimbursement that reduces cash cost
(and correctly feeds Net Profit / the Ad Spend KPI), but they must not inflate
ROAS. With ~80% of spend credited, netting credits made all-time ROAS read 5.38x
when the true ad efficiency is ~1.06x.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_credit import AdCredit
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.order import Order, OrderLine, OrderType
from app.reports.monthly_pnl import compute_monthly_pnl


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


def _order(db, bid, gross):
    o = Order(import_batch_id=bid, tiktok_order_id="O1", placed_at=datetime(2026, 5, 15, 12, 0),
              order_type=OrderType.PAID, status="Shipped", brand="smashbox", gross_sales=gross)
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="SBX-1", quantity=1, unit_price=gross,
                     gross_sales=gross, unit_cogs_snapshot=Decimal("0")))
    db.flush()


def _adspend(db, bid, amount):
    # GMV-Max ad spend now flows through the auto-synced daily-metric feed.
    db.add(GmvMaxDailyMetric(import_batch_id=bid, metric_date=date(2026, 5, 15),
                             cost=amount))
    db.flush()


def _adcredit(db, amount):
    db.add(AdCredit(applied_date=date(2026, 5, 15), year=2026, month=5, amount=amount))
    db.flush()


def test_roas_uses_gross_ad_spend_not_net_of_credits():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))   # net_customer_sales = 1000
        _adspend(db, b.id, Decimal("200.00"))   # gross ad spend = 200
        _adcredit(db, Decimal("150.00"))        # credit (must NOT affect ROAS)
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.net_customer_sales == Decimal("1000.00")
    assert p.total_ad_spend == Decimal("200.00")
    assert p.net_ad_spend == Decimal("50.00")     # Ad Spend KPI stays net — unchanged
    assert p.roas == Decimal("5")                  # 1000 / 200 (gross), NOT 1000/50


def test_managed_roas_uses_gross_ad_spend():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        _adspend(db, b.id, Decimal("200.00"))
        _adcredit(db, Decimal("150.00"))
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    # no smashbox discount, so managed_net_customer_sales == net_customer_sales
    assert p.managed_roas == Decimal("5")


def test_roas_computed_even_when_credits_exceed_spend():
    """April/May case: credits > spend. Net ad spend goes negative, but gross
    ROAS is still well-defined and must be shown (not zeroed)."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        _adspend(db, b.id, Decimal("100.00"))
        _adcredit(db, Decimal("150.00"))          # credit exceeds spend
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.net_ad_spend == Decimal("-50.00")
    assert p.roas == Decimal("10")                 # 1000 / 100 gross


def test_roas_zero_when_no_ad_spend():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.total_ad_spend == Decimal("0")
    assert p.roas == Decimal("0")
