"""Measured depletion rate from the inventory-snapshot time-series.

Locks in: decreases count as depletion, increases are receipts (excluded), the
rate is units/span-days, and order-velocity folds into the SAP/SBX key space
(summing variations that share a physical code) so it lines up with depletion.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.import_batch import _utc_now_naive
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.services.demand.depletion import (
    compute_depletion_rates,
    velocity_by_sap_sku,
)
from app.services.demand.velocity import SkuVelocity


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _snap(db, batch_id, sku, on_hand, days_ago):
    db.add(InventorySnapshot(
        import_batch_id=batch_id, sku=sku, on_hand=on_hand,
        captured_at=_utc_now_naive() - timedelta(days=days_ago),
    ))


def test_depletion_counts_decreases_and_excludes_receipts():
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="s", stored_path="s")
        db.add(b); db.flush()
        # 15-day span: 100 -> 90 (depl 10) -> 95 (receipt 5) -> 80 (depl 15)
        _snap(db, b.id, "SBX-A", 100, 15)
        _snap(db, b.id, "SBX-A", 90, 10)
        _snap(db, b.id, "SBX-A", 95, 5)
        _snap(db, b.id, "SBX-A", 80, 0)
        db.commit()
        out = compute_depletion_rates(db, window_days=60)
    d = out["SBX-A"]
    assert d.units_depleted == 25          # 10 + 15
    assert d.receipts == 5                 # the +5 restock, excluded from depletion
    assert d.span_days == 15
    assert d.daily_depletion == Decimal("1.67")  # 25 / 15


def test_single_snapshot_gets_no_rate():
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        _snap(db, b.id, "SBX-ONLY", 50, 2)
        db.commit()
        out = compute_depletion_rates(db, window_days=60)
    assert "SBX-ONLY" not in out


def test_velocity_folds_into_sap_space_summing_variations():
    """Two TikTok variations of one physical SBX code -> their velocities sum
    into that SBX key, so depletion (per SBX) lines up with total sales."""
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-V", tiktok_sku_id="TT-1", name="V1", brand="smashbox"))
        db.add(Sku(sku="SBX-V", tiktok_sku_id="TT-2", name="V2", brand="smashbox"))
        db.commit()
        vel = {
            "TT-1": SkuVelocity(component_sku="TT-1", units_14d=0, units_60d=60),  # 1.0/day
            "TT-2": SkuVelocity(component_sku="TT-2", units_14d=0, units_60d=120),  # 2.0/day
            "SBX-DIRECT": SkuVelocity(component_sku="SBX-DIRECT", units_14d=0, units_60d=30),  # passthrough
        }
        folded = velocity_by_sap_sku(db, vel)
    assert folded["SBX-V"] == Decimal("3.00")      # 1.0 + 2.0 summed across variations
    assert folded["SBX-DIRECT"] == Decimal("0.50")  # non-catalog key passes through


def test_detail_view_surfaces_measured_depletion():
    """compute_sku_detail_view populates the depletion fields from snapshots."""
    from app.reports.demand_planning import compute_sku_detail_view
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", tiktok_sku_id="SBX-A", name="A",
                   brand="smashbox", unit_cogs=Decimal("5")))
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        _snap(db, b.id, "SBX-A", 100, 10)
        _snap(db, b.id, "SBX-A", 80, 0)
        db.commit()
        view = compute_sku_detail_view(db, "SBX-A")
    assert view is not None
    assert view.measured_depletion is not None
    assert view.measured_depletion.daily_depletion == Decimal("2.00")  # 20 / 10
    assert view.depletion_gap is not None  # no sales → gap == depletion
