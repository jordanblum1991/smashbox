"""Tests for variance-based safety stock with per-SKU service-level tiering.

Replaces the flat `safety_buffer = lead_demand × safety_stock_pct` math with
`safety_stock = z × σ × √lead_time`, where σ is the standard deviation of
the RAW (uncapped) 60-day daily series.

Coverage:
  - High-σ SKU gets a bigger buffer than the flat method.
  - σ uses the RAW daily series (not the spike-capped robust one).
  - Each z-tier is honored (0.90 → 1.28, 0.95 → 1.65, 0.975 → 1.96).
  - Per-SKU service_level overrides the global default.
  - Fallback to flat method when σ is unavailable (no velocity, etc.).
  - Backward-compat: existing callers that don't supply σ still work.
"""
import math
from datetime import date
from decimal import Decimal

import pytest

from app.config import SERVICE_LEVEL_Z_TABLE, settings, z_for_service_level
from app.services.demand.replenishment import (
    ReplenishmentInputs,
    ReplenishmentStatus,
    compute_one,
)
from app.services.demand.velocity import SkuVelocity


AS_OF = date(2026, 5, 20)


def _mk(**overrides) -> ReplenishmentInputs:
    """Sane defaults; override per test."""
    defaults = dict(
        sku_code="SBX-001",
        component_sku="1729000000000000001",
        name="Test Product",
        on_hand=100,
        expected_receipts=0,
        daily_velocity=Decimal("2.00"),
        daily_velocity_14d=Decimal("2.00"),
        lead_time_days=14,
        safety_stock_pct=Decimal("0.10"),     # flat fallback
        cover_days=45,
        overstocked_threshold_days=180,
        moq=0,
        case_pack=0,
        is_reorderable=True,
        unit_cogs=Decimal("5.00"),
    )
    defaults.update(overrides)
    return ReplenishmentInputs(**defaults)


# ---- Z-table lookup --------------------------------------------------------

def test_z_table_exact_values():
    """Spec says: 0.90→1.28, 0.95→1.65, 0.975→1.96."""
    assert z_for_service_level(Decimal("0.90")) == Decimal("1.28")
    assert z_for_service_level(Decimal("0.95")) == Decimal("1.65")
    assert z_for_service_level(Decimal("0.975")) == Decimal("1.96")


def test_z_table_decimal_equality_handles_trailing_zeros():
    """Decimal("0.95") == Decimal("0.950") so the lookup is value-based."""
    assert z_for_service_level(Decimal("0.950")) == Decimal("1.65")


def test_z_table_rejects_unsupported_service_level():
    with pytest.raises(KeyError):
        z_for_service_level(Decimal("0.99"))


# ---- Variance-based safety stock --------------------------------------------

def test_variance_uses_z_sigma_sqrt_lead():
    """Exact math check: σ=2, z=1.65 (95%), lead=14 → 1.65×2×√14 ≈ 12.35 → 12."""
    inp = _mk(
        sigma_daily=Decimal("2.0"),
        z_value=Decimal("1.65"),
        lead_time_days=14,
    )
    r = compute_one(inp, as_of=AS_OF)
    expected = round(1.65 * 2.0 * math.sqrt(14))   # 12.35 → 12
    assert r.safety_stock_units == expected
    assert r.safety_method == "variance"


def test_high_sigma_gets_bigger_buffer_than_flat_method():
    """The whole point: a volatile SKU gets bigger safety than 10% of lead
    demand would yield."""
    # Velocity 2/day, lead 14d → lead_demand = 28. Flat 10% → safety=2.8 → 3.
    flat = compute_one(_mk(), as_of=AS_OF)
    assert flat.safety_method == "flat"
    assert flat.safety_stock_units == 3

    # σ=3 (high relative to mean of 2). Variance: 1.65×3×√14 ≈ 18.5 → 19.
    variance = compute_one(
        _mk(sigma_daily=Decimal("3.0"), z_value=Decimal("1.65")),
        as_of=AS_OF,
    )
    assert variance.safety_method == "variance"
    assert variance.safety_stock_units > flat.safety_stock_units
    # Reorder point grew accordingly: 28 + 19 = 47 vs 28 + 3 = 31.
    assert variance.reorder_point > flat.reorder_point


def test_low_sigma_gets_smaller_buffer_than_flat():
    """Very-smooth SKU: variance gives a smaller buffer than the flat method
    would inflate it to. Useful for steady-demand items where the flat method
    over-buffers."""
    # σ=0.2 (very low). Variance: 1.65×0.2×√14 ≈ 1.23 → 1.
    # Flat: lead_demand × 0.10 = 28 × 0.10 = 2.8 → 3.
    variance = compute_one(
        _mk(sigma_daily=Decimal("0.2"), z_value=Decimal("1.65")),
        as_of=AS_OF,
    )
    flat = compute_one(_mk(), as_of=AS_OF)
    assert variance.safety_stock_units < flat.safety_stock_units


# ---- Service-level tiers ---------------------------------------------------

@pytest.mark.parametrize("service_level,z,expected_safety", [
    (Decimal("0.90"),  Decimal("1.28"), round(1.28 * 2.0 * math.sqrt(14))),
    (Decimal("0.95"),  Decimal("1.65"), round(1.65 * 2.0 * math.sqrt(14))),
    (Decimal("0.975"), Decimal("1.96"), round(1.96 * 2.0 * math.sqrt(14))),
])
def test_each_service_level_tier_yields_expected_safety(service_level, z, expected_safety):
    """Higher service level → larger z → larger safety stock."""
    assert z_for_service_level(service_level) == z
    inp = _mk(sigma_daily=Decimal("2.0"), z_value=z)
    r = compute_one(inp, as_of=AS_OF)
    assert r.safety_stock_units == expected_safety


def test_higher_service_level_buffers_more_than_lower():
    """Sanity: 97.5% should buffer more than 90%, not less."""
    sigma = Decimal("2.0")
    low  = compute_one(_mk(sigma_daily=sigma, z_value=Decimal("1.28")), as_of=AS_OF)
    mid  = compute_one(_mk(sigma_daily=sigma, z_value=Decimal("1.65")), as_of=AS_OF)
    high = compute_one(_mk(sigma_daily=sigma, z_value=Decimal("1.96")), as_of=AS_OF)
    assert low.safety_stock_units < mid.safety_stock_units < high.safety_stock_units


# ---- Fallback when σ unavailable -------------------------------------------

def test_fallback_to_flat_when_sigma_none():
    """A caller that doesn't supply σ (older test, one-off analysis) still
    works via the flat-percentage path."""
    r = compute_one(_mk(), as_of=AS_OF)
    assert r.safety_method == "flat"
    # 28 × 0.10 = 2.8 → 3.
    assert r.safety_stock_units == 3


def test_fallback_to_flat_when_sigma_zero():
    """σ=0 means demand has no variability — but variance math gives a 0
    buffer, which would leave the SKU unprotected. Fall back to flat to
    avoid a too-thin buffer when σ collapses."""
    r = compute_one(
        _mk(sigma_daily=Decimal("0"), z_value=Decimal("1.65")),
        as_of=AS_OF,
    )
    assert r.safety_method == "flat"


def test_fallback_to_flat_when_z_none():
    """σ supplied but z missing — defensive: don't divide by None."""
    r = compute_one(
        _mk(sigma_daily=Decimal("2.0"), z_value=None),
        as_of=AS_OF,
    )
    assert r.safety_method == "flat"


# ---- σ from RAW vs capped (regression guard) -------------------------------

def test_sigma_comes_from_raw_daily_series_not_capped():
    """The cap shrinks σ — using capped σ would under-buffer the spikes
    we're trying to insure against. Build a series with one big spike,
    compute σ on raw vs capped, and assert sigma_daily_raw uses raw."""
    # 59 days at 1 unit, one day at 30 → big spike.
    raw_series = [1] * 59 + [30]
    v = SkuVelocity(
        component_sku="SBX-VOL",
        units_14d=1,
        units_60d=sum(raw_series),
        daily_series_60d=raw_series,
    )

    # σ of raw should be ~3.7 (one big outlier among many low-value days).
    sigma_raw = v.sigma_daily_raw
    # Compute σ of the same series capped — robust_daily_rate clips that 30.
    # Cap formula: max(3 × median_nz, 5 × raw_mean). All 60 days have a value,
    # median nonzero = 1, mean = (59+30)/60 ≈ 1.48. Cap = max(3, 7.42) = 7.42.
    # So clipped series: 59 ones plus 7.42 (clipped from 30).
    capped = [Decimal(x) for x in raw_series[:-1]] + [Decimal("7.42")]
    n = len(capped)
    mean = sum(capped) / Decimal(n)
    var_capped = sum((x - mean) ** 2 for x in capped) / Decimal(n - 1)
    sigma_capped = Decimal(str(math.sqrt(float(var_capped))))

    # The raw σ must be substantially larger than the capped one — that's
    # the entire reason this distinction matters.
    assert sigma_raw > sigma_capped * Decimal("2")


# ---- Per-SKU service_level overrides global --------------------------------

def test_per_sku_service_level_takes_precedence_over_global():
    """Setting a per-SKU service_level means that z applies, not the global
    default. (Exercised by the orchestration layer; here we verify the math
    yields different buffers when passed different z values.)"""
    sigma = Decimal("2.0")
    z_global  = z_for_service_level(settings.demand_service_level_default)
    z_per_sku = z_for_service_level(Decimal("0.975"))   # higher tier
    assert z_per_sku > z_global

    r_global  = compute_one(_mk(sigma_daily=sigma, z_value=z_global), as_of=AS_OF)
    r_per_sku = compute_one(_mk(sigma_daily=sigma, z_value=z_per_sku), as_of=AS_OF)
    assert r_per_sku.safety_stock_units > r_global.safety_stock_units


# ---- Integration: σ flows from velocity → compute_one ----------------------

def test_sku_velocity_exposes_sigma_daily_raw():
    """The SkuVelocity dataclass must expose a sigma_daily_raw the caller
    can read without re-computing from the daily series."""
    v = SkuVelocity(
        component_sku="SBX-X",
        units_14d=14, units_60d=60,
        daily_series_60d=[1] * 60,
    )
    # Perfectly flat: σ = 0.
    assert v.sigma_daily_raw == Decimal("0")

    # Mixed values: σ > 0.
    v2 = SkuVelocity(
        component_sku="SBX-Y",
        units_14d=14, units_60d=60,
        daily_series_60d=[0, 2] * 30,
    )
    assert v2.sigma_daily_raw > Decimal("0")


def test_z_table_contains_only_three_supported_tiers():
    """The spec says support exactly 3 tiers (plus the global default which
    must be one of the 3). Guard against accidental table expansion."""
    assert set(SERVICE_LEVEL_Z_TABLE.keys()) == {
        Decimal("0.90"), Decimal("0.95"), Decimal("0.975"),
    }


# ---- Page-level service_level_override plumbing -------------------------

def test_service_level_override_changes_safety_stock_in_view():
    """Passing service_level_override into compute_demand_planning_view must
    actually change z (and therefore safety_stock_units) for SKUs that go
    through the variance branch."""
    from datetime import datetime, timedelta
    from app.db import Base, SessionLocal, engine
    from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
    from app.models.inventory_snapshot import InventorySnapshot
    from app.models.order import Order, OrderLine, OrderType
    from app.models.sku import Sku
    from app.reports.demand_planning import compute_demand_planning_view

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        today = datetime.now()
        with SessionLocal() as db:
            batch = ImportBatch(
                kind=ImportFileKind.TIKTOK_ORDERS,
                status=ImportBatchStatus.COMPLETED,
                original_filename="seed.csv", stored_path="/tmp/seed.csv",
            )
            db.add(batch); db.flush()
            db.add(Sku(
                sku="SBX-X", tiktok_sku_id="999", brand="smashbox", name="Test",
                unit_cogs=Decimal("10"), lead_time_days=14, is_reorderable=True,
            ))
            # Lumpy series → σ > 0 so variance branch fires.
            for i in range(60):
                qty = 5 if i % 2 == 0 else 1
                placed = today - timedelta(days=60 - i)
                o = Order(import_batch_id=batch.id,
                          tiktok_order_id=f"o-{i}",
                          placed_at=placed,
                          order_type=OrderType.PAID, status="Shipped",
                          brand="smashbox")
                db.add(o); db.flush()
                db.add(OrderLine(order_id=o.id, sku="999", quantity=qty,
                                 unit_cogs_snapshot=Decimal("10")))
            db.add(InventorySnapshot(import_batch_id=batch.id, sku="999",
                                     on_hand=200, captured_at=today))
            db.commit()

            view_90 = compute_demand_planning_view(
                db, service_level_override=Decimal("0.90"))
            view_975 = compute_demand_planning_view(
                db, service_level_override=Decimal("0.975"))

            # Rows are keyed by the physical SKU code (Sku.sku): the planner
            # folds the TikTok-ID velocity + on-hand "999" onto "SBX-X".
            sku_90 = next(r for r in view_90.rows if r.component_sku == "SBX-X")
            sku_975 = next(r for r in view_975.rows if r.component_sku == "SBX-X")

            # The variance method must be active for this SKU.
            assert sku_90.safety_method == "variance"
            assert sku_975.safety_method == "variance"

            # Higher service level → larger z (1.96 vs 1.28) → bigger buffer.
            assert sku_975.safety_stock_units > sku_90.safety_stock_units

            # The view must echo back what was used so the dropdown stays in sync.
            assert view_90.service_level == Decimal("0.90")
            assert view_975.service_level == Decimal("0.975")
    finally:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
