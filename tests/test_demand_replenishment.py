"""Unit tests for the replenishment math.

Covers status classification + suggested-quantity math at known-result
inputs so future tuning has guardrails.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.demand.replenishment import (
    ReplenishmentInputs,
    ReplenishmentStatus,
    compute_one,
)


def _mk(**overrides) -> ReplenishmentInputs:
    """Build a sane-default input for a single SKU; override per test."""
    defaults = dict(
        sku_code="SBX-001",
        component_sku="1729000000000000001",
        name="Test Product",
        on_hand=100,
        expected_receipts=0,
        daily_velocity=Decimal("2.00"),         # 60 d/yr × 2 = 120 units/2mo
        daily_velocity_14d=Decimal("2.00"),
        lead_time_days=14,
        safety_stock_pct=Decimal("0.10"),
        cover_days=45,
        overstocked_threshold_days=180,
        moq=0,
        case_pack=0,
        is_reorderable=True,
        unit_cogs=Decimal("5.00"),
    )
    defaults.update(overrides)
    return ReplenishmentInputs(**defaults)


AS_OF = date(2026, 5, 20)


# ---- Status classification ------------------------------------------------

def test_out_of_stock_when_no_inventory():
    r = compute_one(_mk(on_hand=0), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.OUT_OF_STOCK


def test_at_risk_when_stockout_before_lead_time():
    # velocity 2/day, on-hand 10 → 5 days of supply, lead time 14 → AT_RISK
    r = compute_one(_mk(on_hand=10), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.AT_RISK


def test_reorder_now_when_below_reorder_point():
    # velocity 2/day × 14d × 1.10 = 30.8 → reorder point 31.
    # on-hand 25 < 31 AND >= lead_time*velocity (= 28) → just under reorder point.
    # Wait: 25 < 28 (lead-time demand), so days_of_supply = 12.5 < 14 → AT_RISK.
    # Bump to on-hand 30 (= 15 days, just barely above lead time, but < reorder pt 31).
    r = compute_one(_mk(on_hand=30), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.REORDER_NOW


def test_healthy_when_above_reorder_point():
    r = compute_one(_mk(on_hand=200), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.HEALTHY


def test_overstocked_when_days_supply_exceeds_threshold():
    # velocity 2/day, threshold 180 days → overstock when on-hand > 360
    r = compute_one(_mk(on_hand=500), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.OVERSTOCKED


def test_discontinued_when_not_reorderable():
    r = compute_one(_mk(is_reorderable=False), as_of=AS_OF)
    assert r.status == ReplenishmentStatus.DISCONTINUED
    assert r.suggested_order_qty == 0


def test_no_velocity_when_zero_velocity():
    r = compute_one(_mk(daily_velocity=Decimal("0"), daily_velocity_14d=Decimal("0")),
                    as_of=AS_OF)
    assert r.status == ReplenishmentStatus.NO_VELOCITY


# ---- Order quantity math --------------------------------------------------

def test_suggested_qty_covers_lead_time_plus_cover_days():
    # velocity 2 × (14 + 45) = 118. on-hand 30 → suggest 88.
    r = compute_one(_mk(on_hand=30), as_of=AS_OF)
    assert r.suggested_order_qty == 88


def test_suggested_qty_zero_when_healthy():
    # Plenty of stock → no PO suggested even though math could produce a number.
    r = compute_one(_mk(on_hand=200), as_of=AS_OF)
    assert r.suggested_order_qty == 0


def test_moq_floor_applied():
    # Math would suggest 88, but MOQ is 200 → floor to 200.
    r = compute_one(_mk(on_hand=30, moq=200), as_of=AS_OF)
    assert r.suggested_order_qty == 200


def test_case_pack_rounding_up():
    # 88 raw, case pack 50 → round up to 100.
    r = compute_one(_mk(on_hand=30, case_pack=50), as_of=AS_OF)
    assert r.suggested_order_qty == 100


def test_moq_and_case_pack_both_applied():
    # raw 88 → MOQ 60 (no-op, still 88) → case pack 25 → 100 (round up).
    r = compute_one(_mk(on_hand=30, moq=60, case_pack=25), as_of=AS_OF)
    assert r.suggested_order_qty == 100


def test_expected_receipts_reduces_suggestion():
    # Stays in reorder territory even with receipts (available 30 < reorder pt 31)
    # but the suggestion is reduced by the in-transit units.
    # baseline: on_hand=10, available=10, target 2 × (14+45) = 118 → suggest 108
    # with receipts=20: on_hand=10, available=30, target 118 → suggest 88
    no_receipts = compute_one(_mk(on_hand=10), as_of=AS_OF)
    with_receipts = compute_one(_mk(on_hand=10, expected_receipts=20), as_of=AS_OF)
    assert no_receipts.suggested_order_qty == 108
    assert with_receipts.suggested_order_qty == 88
    assert with_receipts.status == ReplenishmentStatus.REORDER_NOW


def test_investment_equals_qty_times_unit_cogs():
    r = compute_one(_mk(on_hand=30), as_of=AS_OF)
    assert r.investment == (Decimal("88") * Decimal("5.00")).quantize(Decimal("0.01"))


# ---- Reorder-point math ---------------------------------------------------

def test_reorder_point_includes_safety_stock():
    # velocity 2/d × 14 d × 1.10 = 30.8 → rounds to 31
    r = compute_one(_mk(on_hand=100), as_of=AS_OF)
    assert r.reorder_point == 31


def test_reorder_point_scales_with_safety_pct():
    # Same SKU, but 25% safety: velocity 2 × 14 × 1.25 = 35
    r = compute_one(_mk(on_hand=100, safety_stock_pct=Decimal("0.25")), as_of=AS_OF)
    assert r.reorder_point == 35


# ---- Trend ratio ----------------------------------------------------------

def test_trend_ratio_baseline_when_equal_rates():
    r = compute_one(_mk(daily_velocity=Decimal("2"), daily_velocity_14d=Decimal("2")),
                    as_of=AS_OF)
    assert r.trend_ratio == Decimal("1.00")


def test_trend_ratio_above_one_when_accelerating():
    r = compute_one(_mk(daily_velocity=Decimal("2"), daily_velocity_14d=Decimal("3")),
                    as_of=AS_OF)
    assert r.trend_ratio == Decimal("1.50")


def test_trend_ratio_below_one_when_decelerating():
    r = compute_one(_mk(daily_velocity=Decimal("4"), daily_velocity_14d=Decimal("1")),
                    as_of=AS_OF)
    assert r.trend_ratio == Decimal("0.25")


# ---- Days of supply -------------------------------------------------------

def test_days_of_supply_uses_available_inventory():
    # on-hand 30 + expected_receipts 10 → available 40 / velocity 2 = 20 days.
    r = compute_one(_mk(on_hand=30, expected_receipts=10), as_of=AS_OF)
    assert r.days_of_supply == Decimal("20.0")


def test_stockout_date_in_the_future():
    r = compute_one(_mk(on_hand=100), as_of=AS_OF)  # 50 days of supply
    assert r.stockout_date > AS_OF
