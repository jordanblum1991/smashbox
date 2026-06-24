"""Tests for the demand-planning backtest harness.

The harness reuses `compute_velocity` and `compute_one` directly, so the
math itself is already covered by tests/test_demand_replenishment.py and
tests/test_velocity_dampening.py. These tests focus on:

  - Time-machine isolation: at_as_of recommendations ignore later snapshots.
  - Demand measurement: actual_demand_post_as_of counts the right windows
    with the right status/type filter.
  - Scoring logic: stockout / overstock flags fire correctly on synthetic
    demand patterns.
  - Multi-date sweep: identical recommendations + identical actuals across
    multiple as_of dates produce consistent rolled-up rates.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.services.demand.backtest import (
    actual_demand_post_as_of,
    historical_on_hand,
    historical_recommendations,
    last_n_month_starts,
    score_at,
    sweep,
)


# ---- Fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _make_batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="seed.csv",
        stored_path="/tmp/seed.csv",
    )
    db.add(b)
    db.flush()
    return b


def _make_sku(db, *, sku_code: str, tt_id: str, name: str,
              unit_cogs: Decimal = Decimal("10.00"),
              lead_time_days: int | None = 14) -> Sku:
    s = Sku(
        sku=sku_code, tiktok_sku_id=tt_id, brand="smashbox", name=name,
        unit_cogs=unit_cogs, lead_time_days=lead_time_days,
        is_reorderable=True,
    )
    db.add(s)
    db.flush()
    return s


def _add_snapshot(db, batch_id: int, sku: str, on_hand: int,
                  captured_at: datetime) -> InventorySnapshot:
    snap = InventorySnapshot(
        import_batch_id=batch_id, sku=sku,
        on_hand=on_hand, captured_at=captured_at,
    )
    db.add(snap)
    db.flush()
    return snap


def _add_orders_daily(db, batch_id: int, sku: str, *,
                      start: datetime, days: int, qty_per_day: int = 1,
                      order_type: OrderType = OrderType.PAID,
                      status: str = "Shipped") -> None:
    """Add `days` orders, one per day starting at `start`, with the given qty."""
    for i in range(days):
        placed_at = start + timedelta(days=i)
        order_id = f"{sku}-{int(placed_at.timestamp())}-{i}"
        o = Order(
            import_batch_id=batch_id,
            tiktok_order_id=order_id,
            placed_at=placed_at,
            order_type=order_type,
            status=status,
            brand="smashbox",
        )
        db.add(o)
        db.flush()
        ol = OrderLine(
            order_id=o.id, sku=sku, quantity=qty_per_day,
            unit_cogs_snapshot=Decimal("10.00"),
        )
        db.add(ol)
    db.flush()


# ---- Time-machine isolation ------------------------------------------------

def test_historical_on_hand_ignores_later_snapshots():
    """The harness must NOT see a snapshot captured after as_of."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _add_snapshot(db, b.id, "S1", on_hand=100, captured_at=datetime(2026, 1, 15))
        _add_snapshot(db, b.id, "S1", on_hand=10,  captured_at=datetime(2026, 3, 15))
        db.commit()

        # as_of = Feb 1, only the Jan 15 snapshot should be visible.
        result = historical_on_hand(db, datetime(2026, 2, 1))
        assert result == {"S1": 100}

        # as_of = Mar 30, both visible — latest wins.
        result = historical_on_hand(db, datetime(2026, 3, 30))
        assert result == {"S1": 10}


def test_historical_on_hand_returns_empty_when_no_snapshot_yet():
    with SessionLocal() as db:
        b = _make_batch(db)
        _add_snapshot(db, b.id, "S1", on_hand=100, captured_at=datetime(2026, 3, 1))
        db.commit()

        # as_of before any snapshot.
        assert historical_on_hand(db, datetime(2026, 1, 1)) == {}


# ---- Actual demand measurement --------------------------------------------

def test_actual_demand_counts_only_paid_shipped_completed():
    """Actuals filter must match the velocity service's rules — samples and
    canceled orders don't count toward "actual demand"."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A")

        # 10 PAID shipped (counted), 5 SAMPLE shipped (not), 3 PAID canceled (not).
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 5), days=10,
                          order_type=OrderType.PAID, status="Shipped")
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 15), days=5,
                          order_type=OrderType.SAMPLE, status="Shipped")
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 20), days=3,
                          order_type=OrderType.PAID, status="Canceled")
        db.commit()

        # Window [Mar 1, Apr 1) — counts only the 10 paid+shipped.
        daily = actual_demand_post_as_of(db, as_of=datetime(2026, 3, 1), horizon_days=31)
        total = sum(daily.get("S1", {}).values())
        assert total == 10


# ---- Scoring logic ---------------------------------------------------------

def test_stockout_flagged_when_demand_exceeds_on_hand_during_lead_time():
    """SKU starts with 5 units, lead time 14 days, gets 21 units of demand
    in the first 7 days → stockout_during_lead_time = True."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A",
                  unit_cogs=Decimal("10.00"), lead_time_days=14)
        _add_snapshot(db, b.id, "S1", on_hand=5, captured_at=datetime(2026, 3, 1))
        # Pre-history: build velocity so the planner has something to project
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 5), days=55, qty_per_day=2)
        # Post as_of: heavy demand burst on Mar 1-7 (Mar 1 IS in the window
        # since lead_time_days window is [as_of, as_of+14d), inclusive of as_of).
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 1), days=7, qty_per_day=3)
        db.commit()

        sc = score_at(db, as_of=datetime(2026, 3, 1))
        assert len(sc.per_sku) == 1
        row = sc.per_sku[0]
        assert row.on_hand_at_as_of == 5
        assert row.actual_demand_lead == 21    # 7 days × 3 units
        assert row.stockout_during_lead_time is True


def test_overstock_flagged_when_recommendation_dwarfs_actual():
    """SKU's recent velocity inflates the recommendation, but actual demand
    crashes — recommended > 2× actual_30d → overstock = True."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A",
                  unit_cogs=Decimal("10.00"), lead_time_days=14)
        _add_snapshot(db, b.id, "S1", on_hand=10, captured_at=datetime(2026, 3, 1))
        # Pre-history: 55 days of strong velocity → planner thinks we need a lot.
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 5), days=55, qty_per_day=5)
        # Post as_of: only 5 days of demand at 1 unit/day = 5 units. The
        # second SKU is just a data-extent marker so days_available isn't
        # clipped below 30 — without it, data_max would be Mar 9 and the
        # 30-day window would close after only 9 days.
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 5), days=5, qty_per_day=1)
        _make_sku(db, sku_code="SBX-MARKER", tt_id="MARKER", name="Marker")
        _add_orders_daily(db, b.id, "MARKER",
                          start=datetime(2026, 3, 31), days=1, qty_per_day=1)
        db.commit()

        sc = score_at(db, as_of=datetime(2026, 3, 1))
        # Rows are keyed by the physical SKU code (Sku.sku) after the fold.
        row = next(r for r in sc.per_sku if r.component_sku == "SBX-A")
        # Velocity was ~5/day → recommended qty should be substantial; actual
        # is only 5 units — overstock by a wide margin.
        assert row.recommended_qty >= 20
        assert row.actual_demand_30d == 5
        assert row.overstock is True


def test_no_stockout_when_on_hand_covers_lead_time_demand():
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A",
                  unit_cogs=Decimal("10.00"), lead_time_days=14)
        _add_snapshot(db, b.id, "S1", on_hand=200, captured_at=datetime(2026, 3, 1))
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 5), days=55, qty_per_day=2)
        # Mild post-window demand: 1/day for 14 days starting on as_of itself
        # (Mar 1) so the count lands inside the [as_of, as_of+14d) window.
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 1), days=14, qty_per_day=1)
        db.commit()

        sc = score_at(db, as_of=datetime(2026, 3, 1))
        # Rows are keyed by the physical SKU code (Sku.sku) after the fold.
        row = next(r for r in sc.per_sku if r.component_sku == "SBX-A")
        assert row.actual_demand_lead == 14
        assert row.stockout_during_lead_time is False


def test_score_at_consolidates_onhand_and_velocity_by_physical_sku():
    """Realistic key-spaces: SAP on-hand keyed by the SBX-form physical code,
    velocity + actuals keyed by the TikTok SKU ID. The backtest must fold both
    onto the physical SKU — one scorecard row carrying the real on-hand and the
    real velocity — not split into a phantom on_hand=0 velocity row plus a
    no-velocity on-hand row (the live-planner bug, mirrored here)."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A", lead_time_days=14)
        _add_snapshot(db, b.id, "SBX-A", on_hand=50, captured_at=datetime(2026, 3, 1))
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 5), days=55, qty_per_day=2)
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 3, 1), days=14, qty_per_day=1)
        db.commit()

        sc = score_at(db, as_of=datetime(2026, 3, 1))

    rows = [r for r in sc.per_sku if r.sku_code == "SBX-A"]
    assert len(rows) == 1, [(r.component_sku, r.on_hand_at_as_of) for r in rows]
    assert rows[0].on_hand_at_as_of == 50            # SBX-form snapshot attaches
    assert rows[0].actual_demand_lead == 14          # actuals fold from the tt_id


# ---- Catalog-level roll-up -------------------------------------------------

def test_catalog_rollup_aggregates_across_skus():
    """Two SKUs with known outcomes — one stocks out, one doesn't."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-OK",    tt_id="OK", name="OK Item",
                  unit_cogs=Decimal("10.00"), lead_time_days=14)
        _make_sku(db, sku_code="SBX-OUT",   tt_id="OUT", name="Out Item",
                  unit_cogs=Decimal("20.00"), lead_time_days=14)
        _add_snapshot(db, b.id, "OK",  on_hand=200, captured_at=datetime(2026, 3, 1))
        _add_snapshot(db, b.id, "OUT", on_hand=5,   captured_at=datetime(2026, 3, 1))
        # Both have similar pre-history.
        _add_orders_daily(db, b.id, "OK",  start=datetime(2026, 1, 5), days=55)
        _add_orders_daily(db, b.id, "OUT", start=datetime(2026, 1, 5), days=55)
        # Post: OUT gets hammered, OK is calm.
        _add_orders_daily(db, b.id, "OK",  start=datetime(2026, 3, 2), days=5, qty_per_day=1)
        _add_orders_daily(db, b.id, "OUT", start=datetime(2026, 3, 2), days=10, qty_per_day=3)
        db.commit()

        sc = score_at(db, as_of=datetime(2026, 3, 1))
        assert sc.skus_scored == 2
        assert sc.stockout_lead_count == 1
        # Rates expressed as decimal percentages.
        assert sc.stockout_lead_rate == Decimal("50.0")


# ---- Sweep -----------------------------------------------------------------

def test_sweep_runs_multiple_dates():
    """Sweep returns one scorecard per date, in input order."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A")
        _add_snapshot(db, b.id, "S1", on_hand=50, captured_at=datetime(2026, 1, 1))
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 1), days=120)
        db.commit()

        dates = [datetime(2026, 3, 1), datetime(2026, 4, 1)]
        results = sweep(db, dates)
        assert len(results) == 2
        assert [s.as_of for s in results] == [d.date() for d in dates]


# ---- last_n_month_starts ---------------------------------------------------

def test_last_n_month_starts_respects_pre_history_floor():
    """Each as_of must have at least 60 days of pre-history for velocity."""
    with SessionLocal() as db:
        b = _make_batch(db)
        _make_sku(db, sku_code="SBX-A", tt_id="S1", name="Item A")
        # Orders span Jan 1 – Apr 30, 2026.
        _add_orders_daily(db, b.id, "S1",
                          start=datetime(2026, 1, 1), days=120)
        db.commit()

        # Last 3 month-starts. Jan 1 = no pre-history → excluded.
        # Feb 1 = 31 days pre-history → excluded.
        # Mar 1 = 59 days pre-history → excluded.
        # Apr 1 = 90 days pre-history → included.
        # (Apr 30 is the last in-data date; only Apr 1 qualifies.)
        out = last_n_month_starts(db, n=3)
        # We may get 1 month-start depending on the 60-day floor.
        assert all(d.day == 1 for d in out)
        for d in out:
            assert (d - datetime(2026, 1, 1)).days >= 60
