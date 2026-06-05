"""TikTok settlement adjustments on the P&L.

`Adjustment` rows are imported from the settlement file's `Adjustment` sheet.
They represent logistics reimbursements (lost-package credits), TikTok Shop
reimbursements, bill payments, and paired balance/deduction entries that
cancel by construction.

Before this feature, the rows were stored but never displayed. Now they
flow into Net Profit via `MonthlyPnL.tiktok_adjustments_net` summed over
`Adjustment.create_time` in the period window.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from app.db import Base, SessionLocal, engine
from app.models import (
    Adjustment,
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
        kind=ImportFileKind.TIKTOK_SETTLEMENTS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="s.xlsx",
        stored_path="/tmp/s.xlsx",
    )
    db.add(b)
    db.flush()
    return b


def _adj(db, batch_id, create_time, *, adj_id, adj_type, amount,
         reason=None) -> Adjustment:
    a = Adjustment(
        import_batch_id=batch_id,
        adjustment_id=adj_id,
        adjustment_type=adj_type,
        reason=reason,
        amount=amount,
        create_time=create_time,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# 1. Adjustment sums into P&L net_profit
# ---------------------------------------------------------------------------

def test_logistics_reimbursement_adds_to_net_profit():
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 10),
             adj_id="A1", adj_type="Logistics reimbursement",
             amount=Decimal("42.00"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_net == Decimal("42.00")
    # No orders → operating side is $0; adjustments contribute directly.
    assert v.net_profit == Decimal("42.00")


def test_paired_balance_deduction_cancels_to_zero():
    """Net earnings balance + Net earnings deduction with same adjustment_id
    net to $0 by construction."""
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 10),
             adj_id="PAIR1", adj_type="Net earnings balance",
             amount=Decimal("100.00"))
        _adj(db, b.id, datetime(2026, 5, 10),
             adj_id="PAIR1", adj_type="Net earnings deduction",
             amount=Decimal("-100.00"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_net == Decimal("0.00")


def test_negative_adjustment_reduces_net_profit():
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 10),
             adj_id="DEDUCT", adj_type="Some deduction",
             amount=Decimal("-25.00"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_net == Decimal("-25.00")
    assert v.net_profit == Decimal("-25.00")


# ---------------------------------------------------------------------------
# 2. Window scoping by create_time
# ---------------------------------------------------------------------------

def test_adjustment_filtered_by_create_time_into_month():
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 4, 28),     # April
             adj_id="A", adj_type="Logistics reimbursement",
             amount=Decimal("10"))
        _adj(db, b.id, datetime(2026, 5, 1),      # May
             adj_id="B", adj_type="Logistics reimbursement",
             amount=Decimal("20"))
        _adj(db, b.id, datetime(2026, 5, 31, 23, 59, 59),  # May (last instant)
             adj_id="C", adj_type="Logistics reimbursement",
             amount=Decimal("30"))
        _adj(db, b.id, datetime(2026, 6, 1),      # June
             adj_id="D", adj_type="Logistics reimbursement",
             amount=Decimal("40"))
        db.commit()
    with SessionLocal() as db:
        apr = compute_monthly_pnl(db, 2026, 4)
        may = compute_monthly_pnl(db, 2026, 5)
        jun = compute_monthly_pnl(db, 2026, 6)
    assert apr.tiktok_adjustments_net == Decimal("10")
    assert may.tiktok_adjustments_net == Decimal("50")  # 20 + 30
    assert jun.tiktok_adjustments_net == Decimal("40")


def test_adjustment_with_null_create_time_is_skipped():
    """An adjustment with no create_time has no period to attribute — it
    must not appear in any month's P&L."""
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, None,
             adj_id="UNDATED", adj_type="Logistics reimbursement",
             amount=Decimal("99.99"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_net == Decimal("0.00")


# ---------------------------------------------------------------------------
# 3. total_operating_expenses excludes adjustments (it's Other Income)
# ---------------------------------------------------------------------------

def test_total_operating_expenses_excludes_adjustments():
    """Adding adjustments must NOT shrink the displayed Total Operating
    Expenses subtotal — adjustments are Other Income, not a cost."""
    with SessionLocal() as db:
        b_orders = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="o.csv", stored_path="/tmp/o.csv",
        )
        db.add(b_orders); db.flush()
        o = Order(
            import_batch_id=b_orders.id,
            tiktok_order_id="T1", placed_at=datetime(2026, 5, 1, 12, 0),
            order_type=OrderType.PAID, status="Shipped", brand="smashbox",
            gross_sales=Decimal("100"), tiktok_fees=Decimal("10"),
        )
        db.add(o); db.flush()
        db.add(OrderLine(
            order_id=o.id, sku="SBX-001", quantity=1,
            unit_price=Decimal("100"), gross_sales=Decimal("100"),
            unit_cogs_snapshot=Decimal("0"),
        ))
        b_adj = _batch(db)
        _adj(db, b_adj.id, datetime(2026, 5, 10),
             adj_id="A", adj_type="Logistics reimbursement",
             amount=Decimal("30"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # OpEx should be tiktok_fees only ($10), NOT $10 - $30.
    assert v.total_operating_expenses == Decimal("10.00")
    # Net Profit = gross_profit - OpEx + adjustments = 100 - 10 + 30 = 120
    assert v.net_profit == Decimal("120.00")


# ---------------------------------------------------------------------------
# 4. Period-kind aggregation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("period_kind", ["MONTH", "YTD", "YEAR", "CUSTOM", "RANGE"])
def test_adjustments_aggregate_across_period_kinds(period_kind):
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 4, 15),
             adj_id="APR", adj_type="Logistics reimbursement", amount=Decimal("10"))
        _adj(db, b.id, datetime(2026, 5, 15),
             adj_id="MAY", adj_type="TikTok Shop reimbursement", amount=Decimal("20"))
        db.commit()
    with SessionLocal() as db:
        if period_kind == "MONTH":
            v = compute_pnl_view(db, PeriodKind.MONTH, year=2026, month=5)
            expected = Decimal("20")
        elif period_kind == "YTD":
            v = compute_pnl_view(db, PeriodKind.YTD, year=2026, month=5)
            expected = Decimal("30")
        elif period_kind == "YEAR":
            v = compute_pnl_view(db, PeriodKind.YEAR, year=2026)
            expected = Decimal("30")
        elif period_kind == "CUSTOM":
            v = compute_pnl_view(
                db, PeriodKind.CUSTOM,
                start_date=date(2026, 4, 1), end_date=date(2026, 5, 31),
            )
            expected = Decimal("30")
        else:  # RANGE
            v = compute_pnl_view(
                db, PeriodKind.RANGE,
                start_year=2026, start_month=4, end_year=2026, end_month=5,
            )
            expected = Decimal("30")
    assert v.total.tiktok_adjustments_net == expected, period_kind


# ---------------------------------------------------------------------------
# 5. Prod snapshot reconciliation (skip if unmigrated)
# ---------------------------------------------------------------------------

# Expected adjustment totals per month (from the recon against current prod):
PROD_ADJUSTMENTS = {
    (2026, 2): Decimal("1338.03"),
    (2026, 3): Decimal("36.80"),
    (2026, 4): Decimal("224.43"),
    (2026, 5): Decimal("238.04"),
}


# ---------------------------------------------------------------------------
# 6. Per-type breakdown dict
# ---------------------------------------------------------------------------

def test_adjustments_by_type_breakdown_populated():
    """Each adjustment_type sums to its own bucket; total matches the rollup."""
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 1),
             adj_id="L1", adj_type="Logistics reimbursement", amount=Decimal("10"))
        _adj(db, b.id, datetime(2026, 5, 2),
             adj_id="L2", adj_type="Logistics reimbursement", amount=Decimal("15"))
        _adj(db, b.id, datetime(2026, 5, 3),
             adj_id="S1", adj_type="TikTok Shop reimbursement", amount=Decimal("5"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_by_type == {
        "Logistics reimbursement": Decimal("25"),
        "TikTok Shop reimbursement": Decimal("5"),
    }
    # Sum of values == rollup.
    assert sum(v.tiktok_adjustments_by_type.values()) == v.tiktok_adjustments_net


def test_breakdown_sorted_by_abs_amount_desc():
    """Largest absolute value first regardless of sign."""
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 1),
             adj_id="A", adj_type="Small credit", amount=Decimal("10"))
        _adj(db, b.id, datetime(2026, 5, 2),
             adj_id="B", adj_type="Big deduction", amount=Decimal("-500"))
        _adj(db, b.id, datetime(2026, 5, 3),
             adj_id="C", adj_type="Medium credit", amount=Decimal("100"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    ordered_types = list(v.tiktok_adjustments_by_type.keys())
    assert ordered_types == ["Big deduction", "Medium credit", "Small credit"]


def test_breakdown_paired_types_show_separately():
    """Balance and Deduction with same adjustment_id appear as separate
    types in the breakdown — both contribute and net to $0 in the rollup."""
    with SessionLocal() as db:
        b = _batch(db)
        _adj(db, b.id, datetime(2026, 5, 1),
             adj_id="P", adj_type="Net earnings balance", amount=Decimal("100"))
        _adj(db, b.id, datetime(2026, 5, 1),
             adj_id="P", adj_type="Net earnings deduction", amount=Decimal("-100"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_by_type == {
        "Net earnings balance": Decimal("100"),
        "Net earnings deduction": Decimal("-100"),
    }
    assert v.tiktok_adjustments_net == Decimal("0")


def test_breakdown_empty_dict_when_no_adjustments():
    with SessionLocal() as db:
        b_orders = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="o.csv", stored_path="/tmp/o.csv",
        )
        db.add(b_orders); db.flush()
        o = Order(
            import_batch_id=b_orders.id,
            tiktok_order_id="T1", placed_at=datetime(2026, 5, 1),
            order_type=OrderType.PAID, status="Shipped", brand="smashbox",
            gross_sales=Decimal("100"),
        )
        db.add(o); db.flush()
        db.add(OrderLine(
            order_id=o.id, sku="X", quantity=1,
            unit_price=Decimal("100"), gross_sales=Decimal("100"),
            unit_cogs_snapshot=Decimal("0"),
        ))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    assert v.tiktok_adjustments_by_type == {}
    assert v.tiktok_adjustments_net == Decimal("0")


def test_breakdown_merges_across_ytd_months():
    """YTD aggregation sums per-type across months."""
    with SessionLocal() as db:
        b = _batch(db)
        # Mar: Logistics $10
        _adj(db, b.id, datetime(2026, 3, 1),
             adj_id="MAR-L", adj_type="Logistics reimbursement", amount=Decimal("10"))
        # Apr: Logistics $20, Shop $30
        _adj(db, b.id, datetime(2026, 4, 1),
             adj_id="APR-L", adj_type="Logistics reimbursement", amount=Decimal("20"))
        _adj(db, b.id, datetime(2026, 4, 2),
             adj_id="APR-S", adj_type="TikTok Shop reimbursement", amount=Decimal("30"))
        # May: Logistics $5
        _adj(db, b.id, datetime(2026, 5, 1),
             adj_id="MAY-L", adj_type="Logistics reimbursement", amount=Decimal("5"))
        db.commit()
    with SessionLocal() as db:
        v = compute_pnl_view(db, PeriodKind.YTD, year=2026, month=5)
    # Logistics summed: 10 + 20 + 5 = 35. Shop: 30. Sorted abs desc.
    assert list(v.total.tiktok_adjustments_by_type.items()) == [
        ("Logistics reimbursement", Decimal("35")),
        ("TikTok Shop reimbursement", Decimal("30")),
    ]


def test_breakdown_reconciles_to_prod_snapshot_ytd():
    """Production YTD breakdown (Feb-May 2026) sums to the rollup."""
    if not PROD_SNAPSHOT.exists():
        pytest.skip(f"prod snapshot not available at {PROD_SNAPSHOT}")
    eng = create_engine(f"sqlite:///{PROD_SNAPSHOT}", future=True)
    insp = sa_inspect(eng)
    cols = {c["name"] for c in insp.get_columns("orders")}
    if "payment_platform_discount" not in cols:
        pytest.skip("snapshot pre-migration")
    Session = sessionmaker(bind=eng, future=True)
    with Session() as db:
        v = compute_pnl_view(db, PeriodKind.YTD, year=2026, month=5)
    total = v.total
    # Breakdown sums to the rollup.
    assert sum(total.tiktok_adjustments_by_type.values()) == total.tiktok_adjustments_net
    # Known prod types appear (a sanity check; new types are fine).
    assert "Logistics reimbursement" in total.tiktok_adjustments_by_type
    # First entry is the largest absolute value — Bill payment dominates prod.
    first_type = next(iter(total.tiktok_adjustments_by_type))
    assert first_type == "Bill payment (negative balance)"


@pytest.mark.parametrize("year,month,expected", [
    (y, m, t) for (y, m), t in PROD_ADJUSTMENTS.items()
])
def test_prod_snapshot_adjustments_per_month(year, month, expected):
    if not PROD_SNAPSHOT.exists():
        pytest.skip(f"prod snapshot not available at {PROD_SNAPSHOT}")
    eng = create_engine(f"sqlite:///{PROD_SNAPSHOT}", future=True)
    insp = sa_inspect(eng)
    cols = {c["name"] for c in insp.get_columns("orders")}
    if "payment_platform_discount" not in cols:
        pytest.skip("snapshot pre-migration")
    Session = sessionmaker(bind=eng, future=True)
    with Session() as db:
        v = compute_pnl_view(db, PeriodKind.MONTH, year=year, month=month)
    # Allow $0.50 wiggle for any rounding drift; exact match is the goal.
    assert abs(v.total.tiktok_adjustments_net - expected) < Decimal("0.50"), (
        f"{year}-{month:02d}: {v.total.tiktok_adjustments_net} vs expected {expected}"
    )
