"""Tests for cold-start velocity handling.

A SKU sold for the first time within the last N days (default 30) has a
60-day mean polluted by pre-existence zero days. The math re-means over the
observed days only and applies an uplift multiplier — both threshold and
uplift are settings-driven.

Coverage:
  - SkuVelocity.days_observed defaults to 60 (mature).
  - SkuVelocity.daily_observed equals daily_60d_raw for mature SKUs.
  - SkuVelocity.daily_observed > daily_60d_raw when days_observed < 60.
  - compute_first_sold_at_per_component picks the earliest order date per
    component, alias-collapsed, bundle-aware.
  - compute_one cold-start branch: re-means + applies uplift, marks
    velocity_method = "cold_start", forces trend_direction = INSUFFICIENT_DATA.
  - Mature SKUs are unaffected.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.services.demand.replenishment import (
    ReplenishmentInputs,
    TrendDirection,
    compute_one,
)
from app.services.demand.velocity import (
    SkuVelocity,
    compute_first_sold_at_per_component,
    compute_velocity,
)


AS_OF = date(2026, 5, 20)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


# ---- SkuVelocity dataclass: days_observed + daily_observed ---------------

def test_sku_velocity_default_days_observed_is_60():
    v = SkuVelocity(component_sku="X", units_14d=14, units_60d=60,
                    daily_series_60d=[1] * 60)
    assert v.days_observed == 60


def test_sku_velocity_daily_observed_equals_daily_60d_for_mature():
    v = SkuVelocity(component_sku="X", units_14d=14, units_60d=60,
                    daily_series_60d=[1] * 60, days_observed=60)
    assert v.daily_observed == v.daily_60d_raw


def test_sku_velocity_daily_observed_higher_when_cold_start():
    """SKU only 10 days old with 10 sales → daily_observed=1.0, but
    daily_60d_raw=10/60=0.17. Cold-start denominator is the realistic rate."""
    series = [0] * 50 + [1] * 10
    v = SkuVelocity(component_sku="X", units_14d=10, units_60d=10,
                    daily_series_60d=series, days_observed=10)
    assert v.daily_observed == Decimal("1.00")
    assert v.daily_60d_raw == Decimal("0.17")
    assert v.daily_observed > v.daily_60d_raw


def test_sku_velocity_daily_observed_handles_zero_days_safely():
    """Defensive: days_observed=0 shouldn't div-by-zero."""
    v = SkuVelocity(component_sku="X", units_14d=0, units_60d=0,
                    daily_series_60d=[0] * 60, days_observed=0)
    assert v.daily_observed == Decimal("0")


# ---- compute_first_sold_at_per_component DB query ------------------------

def test_first_sold_at_returns_earliest_paid_shipped_date():
    """Pick the earliest matching order across all history."""
    now = datetime(2026, 5, 20, 12, 0)
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv", stored_path="/tmp/seed.csv",
        )
        db.add(batch); db.flush()
        # Two paid+shipped orders, 30 days apart.
        for i, days_ago in enumerate([60, 5]):
            o = Order(import_batch_id=batch.id,
                      tiktok_order_id=f"o-{i}",
                      placed_at=now - timedelta(days=days_ago),
                      order_type=OrderType.PAID, status="Shipped",
                      brand="smashbox")
            db.add(o); db.flush()
            db.add(OrderLine(order_id=o.id, sku="SKU-A", quantity=1,
                             unit_cogs_snapshot=Decimal("5")))
        db.commit()

        result = compute_first_sold_at_per_component(db, alias_map={})
        assert "SKU-A" in result
        assert result["SKU-A"] == (now - timedelta(days=60)).date()


def test_first_sold_at_ignores_canceled_orders():
    """Only Shipped+Completed PAID/PAID_SAMPLE orders count, matching velocity."""
    now = datetime(2026, 5, 20, 12, 0)
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv", stored_path="/tmp/seed.csv",
        )
        db.add(batch); db.flush()
        # Older canceled order — must be ignored.
        o_old = Order(import_batch_id=batch.id, tiktok_order_id="o-old",
                      placed_at=now - timedelta(days=100),
                      order_type=OrderType.PAID, status="Canceled",
                      brand="smashbox")
        db.add(o_old); db.flush()
        db.add(OrderLine(order_id=o_old.id, sku="SKU-B", quantity=1,
                         unit_cogs_snapshot=Decimal("5")))
        # Newer shipped order — must win.
        o_new = Order(import_batch_id=batch.id, tiktok_order_id="o-new",
                      placed_at=now - timedelta(days=20),
                      order_type=OrderType.PAID, status="Shipped",
                      brand="smashbox")
        db.add(o_new); db.flush()
        db.add(OrderLine(order_id=o_new.id, sku="SKU-B", quantity=1,
                         unit_cogs_snapshot=Decimal("5")))
        db.commit()

        result = compute_first_sold_at_per_component(db, alias_map={})
        assert result["SKU-B"] == (now - timedelta(days=20)).date()


# ---- compute_velocity threads first_sold_at into days_observed ------------

def test_compute_velocity_populates_days_observed_from_first_sold_at():
    """When first_sold_at says the SKU was first sold 10 days ago,
    days_observed should be 10 (not 60)."""
    now = datetime(2026, 5, 20, 12, 0)
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv", stored_path="/tmp/seed.csv",
        )
        db.add(batch); db.flush()
        # SKU sold once 5 days ago (cold-start).
        o = Order(import_batch_id=batch.id, tiktok_order_id="o-1",
                  placed_at=now - timedelta(days=5),
                  order_type=OrderType.PAID, status="Shipped",
                  brand="smashbox")
        db.add(o); db.flush()
        db.add(OrderLine(order_id=o.id, sku="SKU-COLD", quantity=3,
                         unit_cogs_snapshot=Decimal("5")))
        db.commit()

        first_sold = compute_first_sold_at_per_component(db, alias_map={})
        velocities = compute_velocity(db, as_of=now, alias_map={},
                                      first_sold_at=first_sold)
        assert "SKU-COLD" in velocities
        v = velocities["SKU-COLD"]
        assert v.days_observed == 5
        # daily_observed = 3 / 5 = 0.6; daily_60d_raw = 3 / 60 = 0.05.
        assert v.daily_observed == Decimal("0.60")
        assert v.daily_60d_raw == Decimal("0.05")


def test_compute_velocity_days_observed_is_60_for_mature_sku():
    """A SKU first sold well before the 60-day window keeps days_observed=60."""
    now = datetime(2026, 5, 20, 12, 0)
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv", stored_path="/tmp/seed.csv",
        )
        db.add(batch); db.flush()
        # First sale 90 days ago — well outside the 60d window.
        o_old = Order(import_batch_id=batch.id, tiktok_order_id="o-old",
                      placed_at=now - timedelta(days=90),
                      order_type=OrderType.PAID, status="Shipped",
                      brand="smashbox")
        db.add(o_old); db.flush()
        db.add(OrderLine(order_id=o_old.id, sku="SKU-MATURE", quantity=1,
                         unit_cogs_snapshot=Decimal("5")))
        # Recent sale (so the SKU appears in the velocity output at all).
        o_new = Order(import_batch_id=batch.id, tiktok_order_id="o-new",
                      placed_at=now - timedelta(days=5),
                      order_type=OrderType.PAID, status="Shipped",
                      brand="smashbox")
        db.add(o_new); db.flush()
        db.add(OrderLine(order_id=o_new.id, sku="SKU-MATURE", quantity=2,
                         unit_cogs_snapshot=Decimal("5")))
        db.commit()

        first_sold = compute_first_sold_at_per_component(db, alias_map={})
        velocities = compute_velocity(db, as_of=now, alias_map={},
                                      first_sold_at=first_sold)
        assert velocities["SKU-MATURE"].days_observed == 60


# ---- compute_one cold-start branch ---------------------------------------

def _mk(**overrides) -> ReplenishmentInputs:
    defaults = dict(
        sku_code="SBX-X", component_sku="X", name="Cold Start",
        on_hand=20, expected_receipts=0,
        daily_velocity=Decimal("0.10"),       # diluted by zero-padding
        daily_velocity_raw=Decimal("0.10"),
        daily_velocity_14d=Decimal("0.50"),
        lead_time_days=14,
        safety_stock_pct=Decimal("0.10"),
        cover_days=45,
        overstocked_threshold_days=180,
        moq=0, case_pack=0, is_reorderable=True,
        unit_cogs=Decimal("5.00"),
    )
    defaults.update(overrides)
    return ReplenishmentInputs(**defaults)


def test_cold_start_replaces_velocity_with_observed_times_uplift():
    """SKU 10 days old with 6 units → daily_observed = 0.6; with default
    uplift 1.5× → v = 0.9. Higher than the diluted daily_velocity=0.1."""
    r = compute_one(
        _mk(days_observed=10, units_observed=6),
        as_of=AS_OF,
    )
    assert r.velocity_method == "cold_start"
    # 0.6 × 1.5 = 0.9
    assert r.daily_velocity == Decimal("0.90")


def test_cold_start_does_not_apply_when_days_observed_at_threshold():
    """STRICT less-than: at days=30 (the default threshold), the SKU graduates
    to mature. days=29 is the last day cold-start applies."""
    r = compute_one(
        _mk(days_observed=30, units_observed=15),
        as_of=AS_OF,
    )
    assert r.velocity_method == "standard"
    # daily_velocity passes through unchanged.
    assert r.daily_velocity == Decimal("0.10")


def test_cold_start_applies_on_last_day_below_threshold():
    """STRICT less-than: at days=29 (one below the 30-day threshold), the
    SKU is still cold-start. Tomorrow it graduates."""
    r = compute_one(
        _mk(days_observed=29, units_observed=15),
        as_of=AS_OF,
    )
    assert r.velocity_method == "cold_start"


def test_cold_start_forces_trend_direction_to_insufficient_data():
    """A 5-day-old SKU has no meaningful 14d-vs-60d trend."""
    r = compute_one(
        _mk(days_observed=5, units_observed=3, daily_velocity_14d=Decimal("1.5")),
        as_of=AS_OF,
    )
    assert r.trend_direction == TrendDirection.INSUFFICIENT_DATA


def test_cold_start_does_not_apply_trend_adjustment():
    """Even if the raw 14d/60d ratio would say ACCELERATING, cold-start SKUs
    skip the trend-blend branch — their entire velocity is already an estimate."""
    r = compute_one(
        _mk(days_observed=5, units_observed=3, daily_velocity_14d=Decimal("2.0")),
        as_of=AS_OF,
    )
    assert r.trend_adjustment_applied is False


def test_mature_sku_retains_standard_velocity_method():
    """A SKU with days_observed = 60 (default) does NOT enter the cold-start
    branch — same behavior as before this change."""
    r = compute_one(
        _mk(
            daily_velocity=Decimal("2.00"),
            daily_velocity_raw=Decimal("2.00"),
            daily_velocity_14d=Decimal("2.00"),
            days_observed=60,
            units_observed=120,
        ),
        as_of=AS_OF,
    )
    assert r.velocity_method == "standard"


def test_cold_start_uplift_is_settings_driven_default_1_5x():
    """Explicit verification: default uplift is 1.5×."""
    r = compute_one(
        _mk(days_observed=10, units_observed=10),  # daily_observed=1.0
        as_of=AS_OF,
    )
    # 1.0 × 1.5 = 1.5
    assert r.daily_velocity == Decimal("1.50")


def test_cold_start_uplift_override_per_input_takes_precedence():
    """Caller-supplied uplift wins over the settings default — supports
    backtests and per-SKU overrides without touching settings."""
    r = compute_one(
        _mk(
            days_observed=10, units_observed=10,
            cold_start_uplift=Decimal("2.0"),
        ),
        as_of=AS_OF,
    )
    # 1.0 × 2.0 = 2.0
    assert r.daily_velocity == Decimal("2.00")


def test_cold_start_then_poisson_for_low_uplifted_velocity():
    """A cold-start SKU with low units → uplifted velocity still < 1.0 →
    Poisson safety stock should fire."""
    r = compute_one(
        _mk(
            days_observed=10, units_observed=3,   # daily_observed=0.3 → uplifted=0.45
            sigma_daily=Decimal("0.5"),
            z_value=Decimal("1.65"),
            service_level=Decimal("0.95"),
        ),
        as_of=AS_OF,
    )
    assert r.velocity_method == "cold_start"
    assert r.safety_method == "poisson"
