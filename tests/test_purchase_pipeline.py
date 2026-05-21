"""Tests for the forward-looking purchase pipeline.

Pins the bucket-assignment logic at the 0 / 30 / 60 / 90 day boundaries
and the SKU-exclusion rules (no-velocity, discontinued, beyond 90d).
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.reports.demand_planning import (
    PurchasePipeline,
    compute_purchase_pipeline,
)
from app.services.demand.replenishment import (
    ReplenishmentResult,
    ReplenishmentStatus,
)


TODAY = date(2026, 5, 21)


def _mk(
    *, sku_code: str = "SBX-X", component_sku: str = "X", name: str = "Item X",
    on_hand: int, daily_velocity: Decimal,
    reorder_point: int = 50, lead_time_days: int = 14,
    status: ReplenishmentStatus = ReplenishmentStatus.HEALTHY,
    suggested_order_qty: int = 0, investment: Decimal = Decimal("0"),
    expected_receipts: int = 0,
) -> ReplenishmentResult:
    """Build a ReplenishmentResult with sensible defaults."""
    return ReplenishmentResult(
        sku_code=sku_code, component_sku=component_sku, name=name,
        on_hand=on_hand, expected_receipts=expected_receipts,
        available=on_hand + expected_receipts,
        daily_velocity=daily_velocity, daily_velocity_14d=daily_velocity,
        trend_ratio=Decimal("1"),
        days_of_supply=Decimal(on_hand) / daily_velocity if daily_velocity > 0 else None,
        stockout_date=None,
        lead_time_days=lead_time_days,
        reorder_point=reorder_point,
        suggested_order_qty=suggested_order_qty,
        investment=investment,
        status=status,
    )


# ---- Bucket boundaries ----------------------------------------------------

def test_overdue_bucket_when_on_hand_at_or_below_reorder_point():
    """If on_hand <= reorder_point at as_of, the SKU is overdue (0 days)."""
    r = _mk(on_hand=30, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.REORDER_NOW,
            suggested_order_qty=100, investment=Decimal("500"))
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.overdue) == 1
    assert p.overdue[0].days_until_reorder == 0
    assert p.overdue[0].order_by_date == TODAY


def test_boundary_30_days_lands_in_next_30():
    """A SKU whose crossing is exactly 30 days out belongs in the 30-day bucket."""
    # on_hand=80, reorder_pt=50 → gap=30; velocity=1 → 30 days until.
    r = _mk(on_hand=80, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.next_30) == 1
    assert p.next_30[0].days_until_reorder == 30
    # Not in overdue or 60/90.
    assert p.overdue == [] and p.next_60 == [] and p.next_90 == []


def test_boundary_31_days_lands_in_next_60():
    """Day 31 is the first slot of the 60-day bucket."""
    r = _mk(on_hand=81, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.next_60) == 1
    assert p.next_60[0].days_until_reorder == 31


def test_boundary_60_days_lands_in_next_60():
    """Day 60 is the last slot of the 60-day bucket."""
    r = _mk(on_hand=110, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.next_60) == 1
    assert p.next_60[0].days_until_reorder == 60


def test_boundary_61_days_lands_in_next_90():
    r = _mk(on_hand=111, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.next_90) == 1
    assert p.next_90[0].days_until_reorder == 61


def test_boundary_90_days_lands_in_next_90():
    r = _mk(on_hand=140, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert len(p.next_90) == 1
    assert p.next_90[0].days_until_reorder == 90


def test_beyond_90_days_excluded():
    """A SKU whose crossing is >90 days out is OFF the pipeline view."""
    r = _mk(on_hand=200, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.OVERSTOCKED)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert p.overdue == [] and p.next_30 == [] and p.next_60 == [] and p.next_90 == []


# ---- Exclusions ------------------------------------------------------------

def test_discontinued_skus_excluded():
    """is_reorderable=False → status=DISCONTINUED → excluded from pipeline."""
    r = _mk(on_hand=10, reorder_point=50, daily_velocity=Decimal("1"),
            status=ReplenishmentStatus.DISCONTINUED)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert p.overdue == [] and p.next_30 == [] and p.next_60 == [] and p.next_90 == []


def test_no_velocity_skus_excluded():
    """A SKU with zero velocity can't be projected — excluded."""
    r = _mk(on_hand=10, reorder_point=50, daily_velocity=Decimal("0"),
            status=ReplenishmentStatus.NO_VELOCITY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert p.overdue == [] and p.next_30 == [] and p.next_60 == [] and p.next_90 == []


# ---- Investment math -------------------------------------------------------

def test_overdue_uses_replenishment_result_investment():
    """For currently-overdue SKUs, the pipeline carries forward whatever
    compute_one already produced (MOQ + case-pack already applied)."""
    r = _mk(on_hand=0, reorder_point=50, daily_velocity=Decimal("2"),
            status=ReplenishmentStatus.OUT_OF_STOCK,
            suggested_order_qty=118, investment=Decimal("590.00"))
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert p.overdue[0].suggested_qty == 118
    assert p.overdue[0].investment == Decimal("590.00")


def test_future_po_projects_qty_from_target_minus_reorder_point():
    """For currently-healthy SKUs, qty = velocity × (lead+cover) − reorder_point."""
    # velocity=1, lead=14, cover=45 → target=59; reorder_pt=50 → qty=9.
    r = _mk(on_hand=80, reorder_point=50, daily_velocity=Decimal("1"),
            lead_time_days=14, status=ReplenishmentStatus.HEALTHY)
    p = compute_purchase_pipeline([r], {}, today=TODAY, cover_days=45)
    assert p.next_30[0].suggested_qty == 9   # 59 − 50


# ---- Sort order ------------------------------------------------------------

def test_within_bucket_sorted_by_order_by_date_ascending():
    rows = [
        _mk(component_sku="LATE",  on_hand=70, reorder_point=50,
            daily_velocity=Decimal("1"), status=ReplenishmentStatus.HEALTHY),
        _mk(component_sku="SOON",  on_hand=55, reorder_point=50,
            daily_velocity=Decimal("1"), status=ReplenishmentStatus.HEALTHY),
        _mk(component_sku="MED",   on_hand=60, reorder_point=50,
            daily_velocity=Decimal("1"), status=ReplenishmentStatus.HEALTHY),
    ]
    p = compute_purchase_pipeline(rows, {}, today=TODAY, cover_days=45)
    order = [i.component_sku for i in p.next_30]
    assert order == ["SOON", "MED", "LATE"]


# ---- Aggregate properties --------------------------------------------------

def test_total_investment_sums_across_buckets():
    rows = [
        _mk(component_sku="O", on_hand=0,   reorder_point=50,
            daily_velocity=Decimal("1"), status=ReplenishmentStatus.OUT_OF_STOCK,
            suggested_order_qty=100, investment=Decimal("100")),
        _mk(component_sku="A", on_hand=75,  reorder_point=50,
            daily_velocity=Decimal("1"), status=ReplenishmentStatus.HEALTHY,
            suggested_order_qty=0,   investment=Decimal("0")),
    ]
    p = compute_purchase_pipeline(rows, {}, today=TODAY, cover_days=45)
    # O contributes $100 in overdue; A is healthy with no unit_cogs in sku_meta
    # so projected investment is $0 — totals must still add cleanly.
    assert p.total_investment == p.overdue_investment + p.next_30_investment \
        + p.next_60_investment + p.next_90_investment


def test_empty_results_produces_empty_pipeline():
    p = compute_purchase_pipeline([], {}, today=TODAY, cover_days=45)
    assert p.overdue == [] and p.next_30 == [] and p.next_60 == [] and p.next_90 == []
    assert p.total_investment == Decimal("0")
    assert p.all_items_sorted == []
