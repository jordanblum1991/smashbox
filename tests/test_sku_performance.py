# tests/test_sku_performance.py
"""Per-SKU sales performance: two-window aggregation, momentum, the 6-status
lifecycle, insights, inactive catalog. Seeds PAID orders + lines; no network."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.sku_performance import compute_sku_performance

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, tiktok_id, code, name):
    db.add(Sku(sku=code, name=name, brand="smashbox", tiktok_sku_id=tiktok_id,
               unit_cogs=Decimal("0")))
    db.flush()


def _order(db, d: date, sku_id, qty, *, gross=None, order_type=OrderType.PAID,
           platform_discount=0, outlandish=0, smashbox=0):
    """One PAID order on day d (noon) with a single line for sku_id."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross if gross is not None else qty * 10)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku_id, quantity=qty,
                     gross_sales=Decimal(str(gross if gross is not None else qty * 10)),
                     platform_discount=Decimal(str(platform_discount)),
                     seller_funded_outlandish=Decimal(str(outlandish)),
                     seller_funded_smashbox=Decimal(str(smashbox))))
    db.flush()


# Window helper: selected = May 16–31 (16 days), prior = Apr 30–May 15.
SEL_START, SEL_END = date(2026, 5, 16), date(2026, 5, 31)


def test_units_net_orders_aggregation():
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        # 2 orders for S1 in window: qty 3 + 2 = 5 units, 2 orders.
        _order(db, date(2026, 5, 20), "S1", 3, gross=100, platform_discount=10,
               outlandish=5, smashbox=3)   # net = 100-10-5-3 = 82
        _order(db, date(2026, 5, 22), "S1", 2, gross=40)  # net = 40
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    row = next(r for r in v.rows if r.sku_id == "S1")
    assert row.units == 5
    assert row.orders == 2
    assert row.net_sales == Decimal("122.00")   # 82 + 40
    assert row.code == "SBX-1" and row.name == "Primer"
    assert v.total_units == 5


def test_momentum_and_status_rising_declining_steady():
    with SessionLocal() as db:
        for sk in ("UP", "DOWN", "FLAT"):
            _sku(db, sk, f"SBX-{sk}", sk)
        # prior window (Apr 30–May 15): give each a baseline of 10 units
        _order(db, date(2026, 5, 10), "UP", 10)
        _order(db, date(2026, 5, 10), "DOWN", 10)
        _order(db, date(2026, 5, 10), "FLAT", 10)
        # selected window (May 16–31): UP=20 (+100%), DOWN=4 (-60%), FLAT=11 (+10%)
        _order(db, date(2026, 5, 20), "UP", 20)
        _order(db, date(2026, 5, 20), "DOWN", 4)
        _order(db, date(2026, 5, 20), "FLAT", 11)
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    by = {r.sku_id: r for r in v.rows}
    assert by["UP"].status == "rising" and by["UP"].momentum.state == "up"
    assert by["DOWN"].status == "declining" and by["DOWN"].momentum.state == "down"
    assert by["FLAT"].status == "steady"
    assert by["UP"].prior_units == 10


def test_new_stalled_inactive_statuses():
    with SessionLocal() as db:
        _sku(db, "NEW", "SBX-N", "New")
        _sku(db, "STALL", "SBX-S", "Stall")
        _sku(db, "DEAD", "SBX-D", "Dead")   # catalog, never sold → inactive
        _order(db, date(2026, 5, 20), "NEW", 5)            # first-ever sale, in window
        _order(db, date(2026, 5, 5), "STALL", 8)           # sold only in prior window
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    by = {r.sku_id: r for r in v.rows}
    assert by["NEW"].status == "new"
    assert by["STALL"].status == "stalled" and by["STALL"].units == 0
    assert v.insights.new_count == 1
    assert v.insights.stalled_count == 1
    # DEAD is inactive (catalog, no sales either window) — not in rows, but counted.
    assert "DEAD" not in by
    assert v.inactive_count == 1
    assert any(r.sku_id == "DEAD" for r in v.inactive_rows)


def test_unmapped_sku_and_paid_only_and_sparkline():
    with SessionLocal() as db:
        # No Sku row for "RAW" → Unmapped.
        _order(db, date(2026, 5, 20), "RAW", 4)
        _order(db, date(2026, 5, 21), "RAW", 2)
        # A SAMPLE order must be excluded.
        _order(db, date(2026, 5, 22), "RAW", 99, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    raw = next(r for r in v.rows if r.sku_id == "RAW")
    assert raw.units == 6                 # 4 + 2; the 99 SAMPLE excluded
    assert raw.code == "Unmapped"
    assert raw.spark != ""                # two days of data → a drawable sparkline


def test_insights_and_sort():
    with SessionLocal() as db:
        for sk in ("A", "B", "C"):
            _sku(db, sk, f"SBX-{sk}", sk)
        _order(db, date(2026, 5, 10), "A", 10)   # prior
        _order(db, date(2026, 5, 10), "B", 10)
        _order(db, date(2026, 5, 20), "A", 30)   # +200% riser, 30 units (top seller)
        _order(db, date(2026, 5, 20), "B", 2)    # -80% faller
        _order(db, date(2026, 5, 20), "C", 5)    # new
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END, sort="units")
    assert v.insights.top_seller.sku_id == "A"
    assert v.insights.biggest_riser.sku_id == "A"
    assert v.insights.biggest_faller.sku_id == "B"
    assert [r.sku_id for r in v.rows][0] == "A"   # sorted by units desc
    v2 = compute_sku_performance(db, start=SEL_START, end=SEL_END, sort="orders")
    assert v2.rows  # sort param accepted
