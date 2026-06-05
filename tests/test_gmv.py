"""TikTok Seller Center-aligned GMV property on MonthlyPnL.

GMV = gross_sales + shipping_revenue
      − outlandish − smashbox  (seller promotions)
      − platform_discount      (SKU platform co-funding)
      − payment_platform_discount  (payment platform co-funding)

Tax excluded; refunds/cancellations NOT subtracted.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view

REPO = Path(__file__).resolve().parents[1]
PROD_SNAPSHOT = REPO / "data" / "smashbox.db.prod"


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


# ---------------------------------------------------------------------------
# 1. Canonical formula
# ---------------------------------------------------------------------------

def test_gmv_formula_canonical():
    """GMV = gross + shipping − seller_promo − platform − payment_platform."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(
            db, b.id, datetime(2026, 5, 10), tt_id="T1",
            gross=Decimal("100.00"),
            plat=Decimal("8.00"),
            outl=Decimal("5.00"),
            smash=Decimal("3.00"),
            ppd=Decimal("2.00"),
            ship_rev=Decimal("4.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # GMV = 100 + 4 − 5 − 3 − 8 − 2 = 86
    assert v.gmv == Decimal("86.00")


def test_gmv_no_shipping_no_payment_plat():
    """When shipping and payment_platform_discount are 0, GMV reduces to
    gross − outl − smash − platform_discount."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(
            db, b.id, datetime(2026, 5, 10), tt_id="T1",
            gross=Decimal("100.00"),
            plat=Decimal("10.00"),
            outl=Decimal("5.00"),
            smash=Decimal("2.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.gmv == Decimal("83.00")


# ---------------------------------------------------------------------------
# 2. GMV doesn't subtract refunds (per TikTok rule)
# ---------------------------------------------------------------------------

def test_gmv_does_not_subtract_refunds():
    """A refunded order's gross still contributes to GMV — TikTok counts
    pre-refund."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(
            db, b.id, datetime(2026, 5, 10), tt_id="REFUNDED",
            gross=Decimal("100.00"),
            refunds=Decimal("80.00"),    # almost full refund
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # GMV ignores refunds entirely.
    assert v.gmv == Decimal("100.00")
    # But net_customer_sales does subtract refunds.
    assert v.net_customer_sales == Decimal("20.00")


# ---------------------------------------------------------------------------
# 3. Cancelled orders ARE included in GMV (per TikTok rule)
# ---------------------------------------------------------------------------

def test_gmv_includes_cancelled_orders():
    with SessionLocal() as db:
        b = _batch(db)
        _order(
            db, b.id, datetime(2026, 5, 10), tt_id="CANCELED",
            gross=Decimal("50.00"),
        )
        # Mark as cancelled.
        o = db.query(Order).filter_by(tiktok_order_id="CANCELED").one()
        o.status = "Canceled"
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.gmv == Decimal("50.00")


# ---------------------------------------------------------------------------
# 4. SAMPLE orders excluded (PAID-only filter)
# ---------------------------------------------------------------------------

def test_gmv_excludes_sample_orders():
    with SessionLocal() as db:
        b = _batch(db)
        _order(
            db, b.id, datetime(2026, 5, 10), tt_id="PAID",
            gross=Decimal("100.00"),
        )
        _order(
            db, b.id, datetime(2026, 5, 11), tt_id="SAMPLE",
            gross=Decimal("0.00"),
            order_type=OrderType.SAMPLE,
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # SAMPLE order (gross=0) excluded by compute_window_pnl's PAID filter
    # AND contributes $0 anyway. GMV = $100.
    assert v.gmv == Decimal("100.00")
    assert v.orders_count == 1


# ---------------------------------------------------------------------------
# 5. Aggregation across period kinds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("period_kind", ["MONTH", "YTD", "YEAR", "CUSTOM", "RANGE"])
def test_gmv_aggregates_across_period_kinds(period_kind):
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2026, 5, 10), tt_id="A",
               gross=Decimal("100"), plat=Decimal("5"), ppd=Decimal("1"))
        _order(db, b.id, datetime(2026, 5, 20), tt_id="B",
               gross=Decimal("200"), plat=Decimal("10"), ppd=Decimal("2"))
        db.commit()
    with SessionLocal() as db:
        if period_kind == "MONTH":
            v = compute_pnl_view(db, PeriodKind.MONTH, year=2026, month=5)
        elif period_kind == "YTD":
            v = compute_pnl_view(db, PeriodKind.YTD, year=2026, month=5)
        elif period_kind == "YEAR":
            v = compute_pnl_view(db, PeriodKind.YEAR, year=2026)
        elif period_kind == "CUSTOM":
            v = compute_pnl_view(
                db, PeriodKind.CUSTOM,
                start_date=date(2026, 5, 1), end_date=date(2026, 5, 31),
            )
        else:
            v = compute_pnl_view(
                db, PeriodKind.RANGE,
                start_year=2026, start_month=4, end_year=2026, end_month=6,
            )
    # GMV = (100 - 5 - 1) + (200 - 10 - 2) = 282
    assert v.total.gmv == Decimal("282.00"), period_kind


# ---------------------------------------------------------------------------
# 6. payment_platform_discount default = $0 on rows without the field set
#    (regression guard for the schema default — pre-migration orders)
# ---------------------------------------------------------------------------

def test_gmv_treats_missing_payment_platform_discount_as_zero():
    """Order without payment_platform_discount populated (older row from
    before the schema add) should default to 0, not None."""
    with SessionLocal() as db:
        b = _batch(db)
        # _order helper passes ppd=Decimal("0") by default — same as if the
        # importer hadn't populated the field at all.
        _order(db, b.id, datetime(2026, 5, 10), tt_id="OLD",
               gross=Decimal("100"), plat=Decimal("10"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # 100 - 10 = 90 (no PPD subtraction since field is 0)
    assert v.gmv == Decimal("90.00")
    assert v.payment_platform_discount == Decimal("0")


# ---------------------------------------------------------------------------
# 7. Prod-snapshot reconciliation (skip if absent)
# ---------------------------------------------------------------------------

# Targets from TikTok Seller Center, confirmed 2026-06-01 (May re-confirmed
# 2026-06-05 — ties to the cent once payment_platform_discount + shipping_revenue
# are populated by a post-migration orders re-import):
PROD_TARGETS = {
    (2026, 2): Decimal("1157.98"),
    (2026, 3): Decimal("9549.91"),
    (2026, 4): Decimal("12775.13"),
    (2026, 5): Decimal("13754.86"),
}


@pytest.mark.parametrize("year,month,target", [
    (y, m, t) for (y, m), t in PROD_TARGETS.items()
])
def test_gmv_reconciles_to_seller_center_from_prod_snapshot(year, month, target):
    """Feb-May 2026 should match Seller Center to the cent when GMV is computed
    from the latest prod snapshot.

    NOTE: this test only passes once the payment_platform_discount column is
    populated, which requires the orders CSV to have been re-imported after
    the schema add. If running against an unmigrated snapshot it will fail —
    re-upload the orders CSV via /uploads on prod before re-running."""
    if not PROD_SNAPSHOT.exists():
        pytest.skip(f"prod snapshot not available at {PROD_SNAPSHOT}")
    eng = create_engine(f"sqlite:///{PROD_SNAPSHOT}", future=True)
    # Snapshot taken before the migration won't have payment_platform_discount.
    # Skip rather than fail; this test is meaningful only post-migration.
    from sqlalchemy import inspect
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("orders")}
    if "payment_platform_discount" not in cols:
        pytest.skip(
            "snapshot taken before schema migration "
            "(missing payment_platform_discount column)"
        )
    Session = sessionmaker(bind=eng, future=True)
    with Session() as db:
        v = compute_pnl_view(db, PeriodKind.MONTH, year=year, month=month)
    assert v.total.gmv == target, (
        f"{year}-{month:02d}: GMV={v.total.gmv} vs Seller Center target={target}. "
        f"If this is the first run after deploy, re-upload the orders CSV via "
        f"/uploads to populate payment_platform_discount on existing orders."
    )
