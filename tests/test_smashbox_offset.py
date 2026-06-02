"""Contra net-zero presentation of the Smashbox-Funded Discount.

The MonthlyPnL stored fields stay settlement-real (Smashbox subtracted) so
reconciliation can continue to tie out against TikTok Seller Center. The
`managed_*` properties add the offset back so the rendered P&L is not
reduced by what Smashbox funds directly. Tests guard:

  - the auto-derived offset always equals the discount magnitude;
  - net_profit-equals-net_profit-if-smashbox-zero invariant;
  - stored fields unchanged (reconciliation tie-out preserved);
  - Outlandish-funded line untouched;
  - May 2026 prod-snapshot reconciliation (skipped if snapshot absent);
  - sample / refund edge cases produce no phantom or residual.
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
from app.reports.monthly_pnl import compute_monthly_pnl, compute_window_pnl
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


def _paid_order(
    db,
    batch_id: int,
    placed_at: datetime,
    *,
    tt_id: str,
    gross: Decimal = Decimal("100.00"),
    platform: Decimal = Decimal("0"),
    outlandish: Decimal = Decimal("0"),
    smashbox: Decimal = Decimal("0"),
    refunds: Decimal = Decimal("0"),
    tiktok_fees: Decimal = Decimal("0"),
    units: int = 1,
    sku: str = "SBX-001",
    unit_cogs_snapshot: Decimal = Decimal("0"),
) -> Order:
    o = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=OrderType.PAID,
        status="Shipped",
        brand="smashbox",
        gross_sales=gross,
        platform_discount_total=platform,
        seller_funded_outlandish=outlandish,
        seller_funded_smashbox=smashbox,
        refunds=refunds,
        tiktok_fees=tiktok_fees,
        tiktok_referral_fee=tiktok_fees,
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(
        order_id=o.id,
        sku=sku,
        quantity=units,
        unit_price=gross / units if units else gross,
        gross_sales=gross,
        unit_cogs_snapshot=unit_cogs_snapshot,
    ))
    db.flush()
    return o


def _sample_order(db, batch_id: int, placed_at: datetime, *, tt_id: str) -> Order:
    """$0-gross sample — by definition no seller-funded split."""
    o = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=OrderType.SAMPLE,
        status="Shipped",
        brand="smashbox",
        gross_sales=Decimal("0"),
    )
    db.add(o)
    db.flush()
    return o


# ---------------------------------------------------------------------------
# 1. Auto-derived offset always equals the discount magnitude.
#    Verified across every period kind we support.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("period_kind", ["MONTH", "YTD", "YEAR", "CUSTOM", "RANGE"])
def test_smashbox_discount_offset_equals_discount(period_kind):
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(
            db, b.id, datetime(2026, 5, 12, 10), tt_id="T1",
            gross=Decimal("500.00"),
            outlandish=Decimal("30.00"),
            smashbox=Decimal("70.00"),
        )
        _paid_order(
            db, b.id, datetime(2026, 5, 20, 14), tt_id="T2",
            gross=Decimal("200.00"),
            outlandish=Decimal("10.00"),
            smashbox=Decimal("15.00"),
        )
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
        else:  # RANGE
            v = compute_pnl_view(
                db, PeriodKind.RANGE,
                start_year=2026, start_month=4, end_year=2026, end_month=6,
            )

    expected = Decimal("85.00")  # 70 + 15
    assert v.total.smashbox_discount == expected
    assert v.total.smashbox_discount_offset == expected, period_kind
    # Pair nets to $0 in every period kind.
    assert v.total.smashbox_discount + (-v.total.smashbox_discount_offset) == Decimal("0")


# ---------------------------------------------------------------------------
# 2. Load-bearing invariant: managed_net_profit equals what net_profit
#    would be if the Smashbox-funded discount were $0.
# ---------------------------------------------------------------------------

def test_managed_net_profit_invariant_when_smashbox_zero():
    """Net profit on the rendered P&L is the same whether Smashbox funded
    $0 or any positive amount of seller discount."""
    # Scenario A: $100 Smashbox-funded discount.
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(
            db, b.id, datetime(2026, 5, 12), tt_id="WITH",
            gross=Decimal("1000.00"),
            outlandish=Decimal("100.00"),
            smashbox=Decimal("100.00"),
            tiktok_fees=Decimal("50.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v_with = compute_monthly_pnl(db, 2026, 5)

    # Reset DB between scenarios so they don't accumulate.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Scenario B: same gross/outlandish/fees, $0 Smashbox-funded.
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(
            db, b.id, datetime(2026, 5, 12), tt_id="WITHOUT",
            gross=Decimal("1000.00"),
            outlandish=Decimal("100.00"),
            smashbox=Decimal("0.00"),
            tiktok_fees=Decimal("50.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v_without = compute_monthly_pnl(db, 2026, 5)

    # The managed view's net_profit must be identical in both scenarios.
    assert v_with.managed_net_profit == v_without.managed_net_profit
    # The settlement-real net_profit (stored) DOES differ by the smashbox amount.
    assert v_with.net_profit + Decimal("100.00") == v_without.net_profit


# ---------------------------------------------------------------------------
# 3. Stored fields unchanged (reconciliation tie-out preserved).
# ---------------------------------------------------------------------------

def test_stored_net_customer_sales_still_subtracts_smashbox():
    """The stored net_customer_sales must remain settlement-real
    (i.e. still has the Smashbox-funded amount subtracted), so the
    reconciliation page can keep tying out against TikTok Seller Center."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(
            db, b.id, datetime(2026, 5, 12), tt_id="T1",
            gross=Decimal("1000.00"),
            platform=Decimal("100.00"),
            outlandish=Decimal("50.00"),
            smashbox=Decimal("75.00"),
            refunds=Decimal("20.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # gross - platform - outlandish - smashbox - refunds == net_customer_sales
    assert v.net_customer_sales == Decimal("755.00")
    # managed_net_customer_sales adds the offset back
    assert v.managed_net_customer_sales == Decimal("830.00")
    # sales_pre_refund (TikTok-aligned) unchanged: net_customer_sales + refunds
    assert v.sales_pre_refund == Decimal("775.00")
    # managed_sales_pre_refund includes the offset
    assert v.managed_sales_pre_refund == Decimal("850.00")


# ---------------------------------------------------------------------------
# 4. Outlandish-funded line is untouched by this change.
# ---------------------------------------------------------------------------

def test_outlandish_discount_unchanged_by_offset_feature():
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(
            db, b.id, datetime(2026, 5, 12), tt_id="T1",
            gross=Decimal("500.00"),
            outlandish=Decimal("42.00"),
            smashbox=Decimal("99.00"),
        )
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # Outlandish stays exactly as imported; the offset feature operates
    # only on the Smashbox-funded portion.
    assert v.outlandish_discount == Decimal("42.00")


# ---------------------------------------------------------------------------
# 5. May 2026 reconciles using the local prod snapshot (skipped if absent).
# ---------------------------------------------------------------------------

def test_may_2026_reconciles_from_prod_snapshot():
    if not PROD_SNAPSHOT.exists():
        pytest.skip(f"prod snapshot not available at {PROD_SNAPSHOT}")
    eng = create_engine(f"sqlite:///{PROD_SNAPSHOT}", future=True)
    # Snapshot taken before the GMV-feature migration won't have
    # payment_platform_discount and compute_pnl_view will SELECT it.
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(eng)
    cols = {c["name"] for c in insp.get_columns("orders")}
    if "payment_platform_discount" not in cols:
        pytest.skip(
            "snapshot taken before GMV feature migration "
            "(missing payment_platform_discount column)"
        )
    Session = sessionmaker(bind=eng, future=True)
    with Session() as db:
        v = compute_pnl_view(db, PeriodKind.MONTH, year=2026, month=5)
    total = v.total
    # Snapshot-dependent values can shift when prod re-imports occur. Assert
    # the load-bearing invariant (offset always equals stored Smashbox)
    # rather than specific dollar amounts that drift with the data.
    assert total.smashbox_discount_offset == total.smashbox_discount
    # Managed Net Customer Sales = stored Net Customer Sales + offset.
    assert (
        total.managed_net_customer_sales - total.net_customer_sales
        == total.smashbox_discount_offset
    )
    # Sanity: positive Smashbox discount in May 2026 (prod has real activity).
    assert total.smashbox_discount > Decimal("0")
    assert total.outlandish_discount > Decimal("0")


# ---------------------------------------------------------------------------
# 6. Sample / refund edge cases — no phantom offset, pair still nets to $0.
# ---------------------------------------------------------------------------

def test_sample_and_refund_edge_cases_offset_stays_correct():
    """A SAMPLE order has gross=$0 and no seller-funded split, so the
    offset must be $0 (no phantom). A refunded PAID order still has the
    original split — the offset auto-derives from the stored discount, so
    the pair still nets to $0 even when refunds are present."""
    with SessionLocal() as db:
        b = _batch(db)
        # 1) Sample order: $0 gross, no split.
        _sample_order(db, b.id, datetime(2026, 5, 3), tt_id="SAMPLE")
        # 2) Refunded paid order with a Smashbox-funded discount.
        _paid_order(
            db, b.id, datetime(2026, 5, 10), tt_id="REFUNDED",
            gross=Decimal("300.00"),
            outlandish=Decimal("15.00"),
            smashbox=Decimal("25.00"),
            refunds=Decimal("60.00"),       # full or partial refund
        )
        # 3) Normal paid order for context.
        _paid_order(
            db, b.id, datetime(2026, 5, 15), tt_id="OK",
            gross=Decimal("200.00"),
            outlandish=Decimal("10.00"),
            smashbox=Decimal("20.00"),
        )
        db.commit()

    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)

    # SAMPLE excluded from PAID-only filter; only the two paid orders count.
    assert v.smashbox_discount == Decimal("45.00")       # 25 + 20
    assert v.smashbox_discount_offset == Decimal("45.00")
    # Deduction + offset == $0 even with a refunded order in the mix.
    assert v.smashbox_discount + (-v.smashbox_discount_offset) == Decimal("0")
    # And the managed net_profit invariant still holds.
    assert v.managed_net_profit == v.net_profit + v.smashbox_discount
