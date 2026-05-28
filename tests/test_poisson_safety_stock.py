"""Tests for Poisson safety stock on slow-mover SKUs.

For SKUs where the effective daily velocity is below settings.demand_slow_mover_threshold
(default 1.0 unit/day), Gaussian z×σ×√L under-buffers because real demand is
Poisson (discrete, skewed). Switch to safety = ppf(service_level, μ·L) − μ·L.

Coverage:
  - poisson_safety_stock() returns the smallest k satisfying P(X ≤ μ·L + k) ≥ SL.
  - Higher service level → larger Poisson buffer.
  - Slow movers go through the Poisson branch (not variance, not flat).
  - Fast movers (μ ≥ 1) continue through variance / flat as before.
  - Service-level fallback: works when only z_value is supplied (reverse-lookup).
  - Pathological inputs (mu_l=0, service_level=0) return 0 cleanly.
"""
import math
from datetime import date
from decimal import Decimal

import pytest

from app.services.demand.replenishment import (
    ReplenishmentInputs,
    compute_one,
    poisson_safety_stock,
)


AS_OF = date(2026, 5, 20)


def _mk(**overrides) -> ReplenishmentInputs:
    defaults = dict(
        sku_code="SBX-SLOW",
        component_sku="999",
        name="Slow Mover",
        on_hand=10,
        expected_receipts=0,
        daily_velocity=Decimal("0.30"),       # slow: 0.3 units/day
        daily_velocity_raw=Decimal("0.30"),
        daily_velocity_14d=Decimal("0.30"),
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


# ---- Pure helper math -----------------------------------------------------

def test_poisson_helper_returns_zero_for_zero_mean():
    assert poisson_safety_stock(Decimal("0"), Decimal("0.95")) == 0


def test_poisson_helper_returns_zero_for_zero_service_level():
    assert poisson_safety_stock(Decimal("5"), Decimal("0")) == 0


def test_poisson_helper_higher_sl_yields_bigger_buffer():
    """Stronger service guarantee → bigger buffer for the same μ·L."""
    mu_l = Decimal("5.0")
    s_90 = poisson_safety_stock(mu_l, Decimal("0.90"))
    s_95 = poisson_safety_stock(mu_l, Decimal("0.95"))
    s_99 = poisson_safety_stock(mu_l, Decimal("0.99"))
    assert s_90 <= s_95 <= s_99
    assert s_99 > s_90  # must strictly grow at extremes


def test_poisson_helper_matches_known_value_at_mu_l_4_sl_95():
    """μ·L = 4, SL = 95% → Poisson PPF = 7 (cumulative P(X≤7) ≈ 0.9489 fails,
    P(X≤8) ≈ 0.9786 succeeds — actually verify against scipy reference if
    available, otherwise hand-computed: cumulative for μ=4 is approximately
    [0.018, 0.092, 0.238, 0.433, 0.629, 0.785, 0.889, 0.949, 0.979] for
    k=0..8, so the smallest k where cumulative ≥ 0.95 is k=8 → safety = 8-4 = 4)."""
    safety = poisson_safety_stock(Decimal("4.0"), Decimal("0.95"))
    assert safety == 4


def test_poisson_helper_safety_clamped_to_zero():
    """If ppf returns k smaller than μ·L (mathematically possible at low SL),
    the helper returns 0 — safety stock can't go negative."""
    # μ·L = 5 with very low SL → ppf could be 2; safety = 2 - 5 = -3 → clamp to 0.
    safety = poisson_safety_stock(Decimal("5.0"), Decimal("0.10"))
    assert safety == 0


# ---- Integration via compute_one -----------------------------------------

def test_slow_mover_takes_poisson_path():
    """v=0.3 < 1.0 threshold → Poisson branch fires."""
    r = compute_one(
        _mk(
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "poisson"


def test_fast_mover_does_not_take_poisson_path():
    """v=2.0 >= 1.0 → variance branch (or flat if no σ)."""
    r = compute_one(
        _mk(
            daily_velocity=Decimal("2.00"),
            daily_velocity_raw=Decimal("2.00"),
            daily_velocity_14d=Decimal("2.00"),
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "variance"


def test_slow_mover_at_threshold_boundary_does_not_take_poisson():
    """STRICT less-than: v at exactly 1.00 is NOT a slow mover → variance."""
    r = compute_one(
        _mk(
            daily_velocity=Decimal("1.00"),
            daily_velocity_raw=Decimal("1.00"),
            daily_velocity_14d=Decimal("1.00"),
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "variance"


def test_slow_mover_just_below_threshold_takes_poisson():
    """STRICT less-than: v at 0.99 IS a slow mover → Poisson."""
    r = compute_one(
        _mk(
            daily_velocity=Decimal("0.99"),
            daily_velocity_raw=Decimal("0.99"),
            daily_velocity_14d=Decimal("0.99"),
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "poisson"


def test_poisson_falls_back_to_variance_when_no_service_level_resolves():
    """Slow mover but no service_level AND z_value isn't in the canonical
    table → can't compute Poisson PPF → fall through to variance/flat."""
    r = compute_one(
        _mk(
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.99"),   # not in SERVICE_LEVEL_Z_TABLE
            service_level=None,
        ),
        as_of=AS_OF,
    )
    # Variance branch wins because σ > 0 and z is supplied (even though z
    # doesn't resolve to a canonical SL).
    assert r.safety_method == "variance"


def test_poisson_service_level_reverse_lookup_from_z_value():
    """When only z_value is supplied (no explicit service_level), reverse
    lookup from the canonical table should still let Poisson fire."""
    r = compute_one(
        _mk(
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),   # exact match for SL=0.95
            service_level=None,
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "poisson"


def test_poisson_buffer_is_reasonable_for_slow_mover():
    """v=0.3, L=14 → μ·L=4.2. SL=0.95 → Poisson PPF≈8 → safety ≈ 4 units.
    Compare to variance: 1.65 × σ × √14 — with σ=0.5 that's ~3 units.
    Both are in the same ballpark for this profile, but the math is
    qualitatively different."""
    r = compute_one(
        _mk(
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.safety_method == "poisson"
    # μ·L = 0.3 × 14 = 4.2 → ppf(0.95, 4.2) ≈ 8 → safety ≈ 4.
    assert 2 <= r.safety_stock_units <= 7


def test_poisson_higher_sl_for_same_sku_buffers_more():
    """End-to-end check: same slow-mover SKU, two different service levels,
    the higher level must produce a larger Poisson buffer."""
    r_90 = compute_one(
        _mk(z_value=Decimal("1.28"), service_level=Decimal("0.90")),
        as_of=AS_OF,
    )
    r_975 = compute_one(
        _mk(z_value=Decimal("1.96"), service_level=Decimal("0.975")),
        as_of=AS_OF,
    )
    assert r_90.safety_method == "poisson"
    assert r_975.safety_method == "poisson"
    assert r_975.safety_stock_units >= r_90.safety_stock_units


def test_existing_callers_without_service_level_still_work():
    """Back-compat: a caller that doesn't pass service_level or z_value to a
    slow-mover SKU falls through to the flat-percent branch as before."""
    r = compute_one(_mk(), as_of=AS_OF)
    # No sigma_daily, no z_value, no service_level → flat.
    assert r.safety_method == "flat"
