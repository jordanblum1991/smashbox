"""Tests for trend-adjusted reorder point.

When 14d velocity is materially above the 60d baseline (trend_ratio above
settings.demand_trend_acceleration_threshold), the ROP base velocity is
blended with the 14d rate. ASYMMETRIC: deceleration does NOT shrink ROP —
stocking out on a recovery is worse than carrying capital on a temporary dip.

Coverage:
  - Stable SKU (ratio ≈ 1.0) → no adjustment, ROP unchanged.
  - Accelerating SKU (ratio > threshold) → adjustment applied, ROP grows.
  - Decelerating SKU (ratio < 1/threshold) → trend_direction = DECELERATING
    but ROP unchanged.
  - SOQ (suggested order quantity) is NOT affected by the trend blend.
  - days_of_supply is NOT affected by the trend blend (still uses v_raw).
  - trend_adjustment_applied flag is set iff the branch fired.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.demand.replenishment import (
    ReplenishmentInputs,
    TrendDirection,
    compute_one,
)


AS_OF = date(2026, 5, 20)


def _mk(**overrides) -> ReplenishmentInputs:
    defaults = dict(
        sku_code="SBX-T",
        component_sku="888",
        name="Trend Test",
        on_hand=100,
        expected_receipts=0,
        daily_velocity=Decimal("2.00"),
        daily_velocity_raw=Decimal("2.00"),
        daily_velocity_14d=Decimal("2.00"),
        lead_time_days=14,
        safety_stock_pct=Decimal("0.10"),
        cover_days=45,
        overstocked_threshold_days=180,
        moq=0, case_pack=0,
        is_reorderable=True,
        unit_cogs=Decimal("5.00"),
    )
    defaults.update(overrides)
    return ReplenishmentInputs(**defaults)


# ---- Direction classification ---------------------------------------------

def test_stable_when_ratios_equal():
    r = compute_one(_mk(), as_of=AS_OF)
    assert r.trend_direction == TrendDirection.STABLE
    assert r.trend_adjustment_applied is False


def test_accelerating_when_14d_far_above_60d():
    r = compute_one(
        _mk(daily_velocity_14d=Decimal("4.00")),  # ratio 2.0
        as_of=AS_OF,
    )
    assert r.trend_direction == TrendDirection.ACCELERATING


def test_decelerating_when_14d_far_below_60d():
    r = compute_one(
        _mk(daily_velocity_14d=Decimal("0.50")),  # ratio 0.25
        as_of=AS_OF,
    )
    assert r.trend_direction == TrendDirection.DECELERATING


def test_stable_just_below_acceleration_threshold():
    """Default threshold 1.2 — ratio 1.15 stays STABLE."""
    r = compute_one(
        _mk(daily_velocity_14d=Decimal("2.30")),  # ratio 1.15
        as_of=AS_OF,
    )
    assert r.trend_direction == TrendDirection.STABLE


def test_threshold_exactly_is_stable_not_accelerating():
    """STRICT inequality: ratio = exactly 1.2 is STABLE, not ACCELERATING.
    Per spec — only ratios strictly past the threshold fire the branch."""
    r = compute_one(
        _mk(daily_velocity_14d=Decimal("2.40")),  # ratio 1.20 exactly
        as_of=AS_OF,
    )
    assert r.trend_ratio == Decimal("1.20")
    assert r.trend_direction == TrendDirection.STABLE
    assert r.trend_adjustment_applied is False


def test_accelerating_just_above_threshold():
    """Ratio 1.21 (just past 1.20) IS ACCELERATING."""
    r = compute_one(
        _mk(daily_velocity_14d=Decimal("2.42")),  # ratio 1.21
        as_of=AS_OF,
    )
    assert r.trend_ratio == Decimal("1.21")
    assert r.trend_direction == TrendDirection.ACCELERATING
    assert r.trend_adjustment_applied is True


def test_decel_threshold_strict_too():
    """Decel threshold is 1/1.2 = 0.8333... — quantized trend_ratios that
    fall on either side classify correctly under STRICT inequality.

    Trend is quantized to 2 decimal places, so we can land just-above and
    just-below the 0.8333 boundary with clean inputs:
      - 14d = 1.68, v_raw = 2.00 → ratio 0.84 → 0.84 > 0.8333 → STABLE
      - 14d = 1.66, v_raw = 2.00 → ratio 0.83 → 0.83 < 0.8333 → DECELERATING
    """
    r_above = compute_one(_mk(daily_velocity_14d=Decimal("1.68")), as_of=AS_OF)
    r_below = compute_one(_mk(daily_velocity_14d=Decimal("1.66")), as_of=AS_OF)
    assert r_above.trend_ratio == Decimal("0.84")
    assert r_below.trend_ratio == Decimal("0.83")
    assert r_above.trend_direction == TrendDirection.STABLE
    assert r_below.trend_direction == TrendDirection.DECELERATING


# ---- ROP adjustment: ASYMMETRIC ------------------------------------------

def test_acceleration_grows_reorder_point():
    """ROP must grow when accelerating — that's the whole point."""
    stable = compute_one(_mk(), as_of=AS_OF)
    accel  = compute_one(_mk(daily_velocity_14d=Decimal("4.00")), as_of=AS_OF)
    assert accel.trend_adjustment_applied is True
    assert accel.reorder_point > stable.reorder_point


def test_deceleration_does_NOT_shrink_reorder_point():
    """ASYMMETRIC: decelerating SKUs keep their full ROP. Recovery risk is
    worse than slack capital."""
    stable = compute_one(_mk(), as_of=AS_OF)
    decel  = compute_one(_mk(daily_velocity_14d=Decimal("0.50")), as_of=AS_OF)
    assert decel.trend_direction == TrendDirection.DECELERATING
    assert decel.trend_adjustment_applied is False
    # ROP must NOT have shrunk versus stable.
    assert decel.reorder_point == stable.reorder_point


def test_trend_adjustment_flag_only_when_accelerating():
    """The flag must be False for STABLE and DECELERATING; True only for ACCELERATING."""
    cases = [
        (Decimal("2.00"), False),   # stable
        (Decimal("0.50"), False),   # decelerating
        (Decimal("4.00"), True),    # accelerating
    ]
    for v_14, expected in cases:
        r = compute_one(_mk(daily_velocity_14d=v_14), as_of=AS_OF)
        assert r.trend_adjustment_applied is expected, f"v_14={v_14}"


# ---- Side-effects on other math: must be ISOLATED to ROP -----------------

def test_acceleration_does_NOT_change_suggested_qty():
    """SOQ uses v (cold-start-aware but NOT trend-blended). We don't chase a
    14-day spike across 60 days of cover."""
    stable = compute_one(_mk(on_hand=10), as_of=AS_OF)
    accel  = compute_one(_mk(on_hand=10, daily_velocity_14d=Decimal("4.00")), as_of=AS_OF)
    assert accel.suggested_order_qty == stable.suggested_order_qty


def test_acceleration_does_NOT_change_days_of_supply():
    """days_of_supply uses v_raw, never v_for_rop. Pessimistic risk signal
    stays pessimistic."""
    stable = compute_one(_mk(on_hand=10), as_of=AS_OF)
    accel  = compute_one(_mk(on_hand=10, daily_velocity_14d=Decimal("4.00")), as_of=AS_OF)
    assert accel.days_of_supply == stable.days_of_supply


# ---- Insufficient data -----------------------------------------------------

def test_insufficient_data_when_no_velocity():
    """A SKU with v_raw <= 0 returns early with INSUFFICIENT_DATA."""
    r = compute_one(
        _mk(daily_velocity=Decimal("0"), daily_velocity_raw=Decimal("0"),
            daily_velocity_14d=Decimal("0")),
        as_of=AS_OF,
    )
    assert r.trend_direction == TrendDirection.INSUFFICIENT_DATA
