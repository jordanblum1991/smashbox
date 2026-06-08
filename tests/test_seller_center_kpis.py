"""Seller Center KPI group — figures sourced to match TikTok exactly.

GMV / Orders / Items sold / AOV come from TikTok's own Shop Analytics daily
export (`TikTokDailyMetric`) summed over the period — exact by construction.
SKU orders is computed from order lines (Σ distinct SKU per paid order) since
it isn't in the export. Coverage metadata (`as_of`, `complete`) keeps a stale
in-progress month from being mistaken for truth.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.reports.seller_center_kpis import compute_seller_center_kpis


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db, kind=ImportFileKind.TIKTOK_ORDERS) -> ImportBatch:
    b = ImportBatch(kind=kind, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _metric(db, bid, d, *, gmv, orders, items):
    db.add(TikTokDailyMetric(import_batch_id=bid, metric_date=d,
                             gmv=Decimal(str(gmv)), orders=orders, items_sold=items))
    db.flush()


def _order(db, bid, oid, skus, *, order_type=OrderType.PAID, placed=datetime(2026, 5, 15, 12, 0)):
    o = Order(import_batch_id=bid, tiktok_order_id=oid, placed_at=placed,
              order_type=order_type, status="Shipped", brand="smashbox",
              gross_sales=Decimal("10"))
    db.add(o); db.flush()
    for sku in skus:
        db.add(OrderLine(order_id=o.id, sku=sku, quantity=1,
                         unit_price=Decimal("10"), gross_sales=Decimal("10"),
                         unit_cogs_snapshot=Decimal("0")))
    db.flush()


MAY_START, MAY_END = datetime(2026, 5, 1), datetime(2026, 6, 1)


def test_gmv_orders_items_summed_from_daily_export():
    with SessionLocal() as db:
        b = _batch(db, ImportFileKind.TIKTOK_ANALYTICS)
        _metric(db, b.id, date(2026, 5, 10), gmv="100.00", orders=4, items=5)
        _metric(db, b.id, date(2026, 5, 20), gmv="200.00", orders=6, items=7)
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.gmv == Decimal("300.00")
    assert k.orders == 10
    assert k.items_sold == 12


def test_aov_is_gmv_over_orders():
    with SessionLocal() as db:
        b = _batch(db, ImportFileKind.TIKTOK_ANALYTICS)
        _metric(db, b.id, date(2026, 5, 10), gmv="100.00", orders=4, items=5)
        _metric(db, b.id, date(2026, 5, 20), gmv="200.00", orders=6, items=7)
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.aov == Decimal("30.00")          # 300 / 10


def test_aov_zero_when_no_orders():
    with SessionLocal() as db:
        b = _batch(db, ImportFileKind.TIKTOK_ANALYTICS)
        _metric(db, b.id, date(2026, 5, 10), gmv="0.00", orders=0, items=0)
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.aov == Decimal("0")


def test_sku_orders_counts_distinct_sku_per_paid_order():
    with SessionLocal() as db:
        ob = _batch(db)
        _order(db, ob.id, "A", ["X", "Y"])          # 2 distinct
        _order(db, ob.id, "B", ["X", "X"])          # duplicate SKU -> 1 distinct
        _order(db, ob.id, "C", ["Z"])               # 1
        _order(db, ob.id, "S", ["X", "Y", "Z"], order_type=OrderType.SAMPLE)  # excluded
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.sku_orders == 4                          # 2 + 1 + 1, sample excluded


def test_coverage_partial_when_export_does_not_reach_period_end():
    with SessionLocal() as db:
        b = _batch(db, ImportFileKind.TIKTOK_ANALYTICS)
        _metric(db, b.id, date(2026, 5, 10), gmv="100.00", orders=4, items=5)
        _metric(db, b.id, date(2026, 5, 20), gmv="200.00", orders=6, items=7)
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.as_of == date(2026, 5, 20)
    assert k.days_covered == 2
    assert k.complete is False                        # data stops at 05-20, month ends 05-31


def test_coverage_complete_when_export_reaches_period_end():
    with SessionLocal() as db:
        b = _batch(db, ImportFileKind.TIKTOK_ANALYTICS)
        _metric(db, b.id, date(2026, 5, 31), gmv="50.00", orders=1, items=1)
        db.commit()
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.as_of == date(2026, 5, 31)
    assert k.complete is True


def test_empty_period_has_no_data():
    with SessionLocal() as db:
        k = compute_seller_center_kpis(db, MAY_START, MAY_END)
    assert k.gmv == Decimal("0")
    assert k.orders == 0
    assert k.items_sold == 0
    assert k.sku_orders == 0
    assert k.aov == Decimal("0")
    assert k.as_of is None
    assert k.complete is False
    assert k.has_data is False
