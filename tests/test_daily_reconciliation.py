"""Daily sales reconciliation compares OUR GMV against TikTok's reported GMV.

The daily table's "ours" column must be computed the same way TikTok defines
GMV — gross + shipping − seller promos − platform co-funding (SKU + payment) —
so the per-day variance reflects a *real* discrepancy, not the structural
shipping/co-funding gap between pre-refund Sales and GMV.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.reports.reconciliation import daily_sales_reconciliation


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="t.csv",
        stored_path="/tmp/t.csv",
    )
    db.add(b)
    db.flush()
    return b


def _order(db, batch_id, placed_at, *, tt_id, gross=Decimal("100"),
           plat=Decimal("0"), outl=Decimal("0"), smash=Decimal("0"),
           ppd=Decimal("0"), ship_rev=Decimal("0"), refunds=Decimal("0"),
           order_type=OrderType.PAID) -> Order:
    o = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=order_type,
        status="Shipped",
        brand="smashbox",
        gross_sales=gross,
        platform_discount_total=plat,
        seller_funded_outlandish=outl,
        seller_funded_smashbox=smash,
        payment_platform_discount=ppd,
        shipping_revenue=ship_rev,
        refunds=refunds,
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(
        order_id=o.id, sku="SBX-001", quantity=1,
        unit_price=gross, gross_sales=gross,
        unit_cogs_snapshot=Decimal("0"),
    ))
    db.flush()
    return o


def _tt_metric(db, batch_id, metric_date, gmv):
    db.add(TikTokDailyMetric(
        import_batch_id=batch_id,
        metric_date=metric_date,
        gmv=gmv,
    ))
    db.flush()


def test_daily_our_figure_is_gmv_not_pre_refund_sales():
    """The per-day "ours" figure includes shipping and subtracts payment
    platform discount — i.e. it's GMV, not pre-refund product Sales."""
    with SessionLocal() as db:
        b = _batch(db)
        # GMV = 100 + 4 (ship) − 5 (outl) − 3 (smash) − 8 (plat) − 2 (ppd) = 86
        # Pre-refund Sales (the OLD figure) would be 100 − 8 − 5 − 3 = 84.
        _order(db, b.id, datetime(2026, 5, 10, 12, 0), tt_id="T1",
               gross=Decimal("100.00"), plat=Decimal("8.00"),
               outl=Decimal("5.00"), smash=Decimal("3.00"),
               ppd=Decimal("2.00"), ship_rev=Decimal("4.00"))
        db.commit()
    with SessionLocal() as db:
        rows = daily_sales_reconciliation(db, 2026, 5)
    assert len(rows) == 1
    assert rows[0].gmv == Decimal("86.00")


def test_daily_variance_zero_when_our_gmv_matches_tiktok_gmv():
    """When TikTok's reported daily GMV equals our GMV-equivalent, the
    variance is exactly zero — no false amber flag from the shipping/
    co-funding definitional gap."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2026, 5, 10, 12, 0), tt_id="T1",
               gross=Decimal("100.00"), plat=Decimal("8.00"),
               outl=Decimal("5.00"), smash=Decimal("3.00"),
               ppd=Decimal("2.00"), ship_rev=Decimal("4.00"))
        _tt_metric(db, b.id, date(2026, 5, 10), gmv=Decimal("86.00"))
        db.commit()
    with SessionLocal() as db:
        rows = daily_sales_reconciliation(db, 2026, 5)
    assert len(rows) == 1
    assert rows[0].tiktok_gmv == Decimal("86.00")
    assert rows[0].tiktok_variance == Decimal("0.00")


def test_daily_gmv_ignores_refunds_but_net_customer_sales_subtracts_them():
    """GMV is pre-refund; net_customer_sales still nets refunds out."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2026, 5, 10, 12, 0), tt_id="T1",
               gross=Decimal("100.00"), refunds=Decimal("30.00"))
        db.commit()
    with SessionLocal() as db:
        rows = daily_sales_reconciliation(db, 2026, 5)
    assert rows[0].gmv == Decimal("100.00")
    assert rows[0].refunds == Decimal("30.00")
    assert rows[0].net_customer_sales == Decimal("70.00")
