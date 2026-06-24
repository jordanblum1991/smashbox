"""Suppress noise rows on the demand planner.

A SKU that's in the SAP feed but has no catalog Sku row, zero on-hand, and no
velocity renders as a blank line (no product name, nothing actionable). These
carry no signal and must not appear. Unmapped SKUs that DO have stock or
velocity are still surfaced — there's something real to act on / map.
"""
from datetime import datetime

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.reports.demand_planning import compute_demand_planning_view

AS_OF = datetime(2026, 6, 23, 12, 0, 0)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(
        kind=ImportFileKind.INVENTORY_SNAPSHOT,
        status=ImportBatchStatus.COMPLETED,
        original_filename="seed", stored_path="",
    )
    db.add(b)
    db.flush()
    return b


def test_unmapped_zero_stock_zero_velocity_row_is_suppressed():
    """SBX-C6P401-style row: snapshot exists at on_hand=0, no Sku, no sales →
    a blank line. It must be dropped from the planner."""
    with SessionLocal() as db:
        b = _batch(db)
        db.add(InventorySnapshot(import_batch_id=b.id, sku="SBX-C6P401",
                                 on_hand=0, captured_at=datetime(2026, 6, 24)))
        db.commit()
        view = compute_demand_planning_view(db, as_of=AS_OF)

    assert not any(r.component_sku == "SBX-C6P401" for r in view.rows)


def test_unmapped_row_with_on_hand_is_still_shown():
    """An unmapped SKU that actually has stock is NOT noise — surface it so it
    can be investigated/mapped (guard against over-filtering)."""
    with SessionLocal() as db:
        b = _batch(db)
        db.add(InventorySnapshot(import_batch_id=b.id, sku="SBX-MYSTERY",
                                 on_hand=12, captured_at=datetime(2026, 6, 24)))
        db.commit()
        view = compute_demand_planning_view(db, as_of=AS_OF)

    row = next((r for r in view.rows if r.component_sku == "SBX-MYSTERY"), None)
    assert row is not None
    assert row.on_hand == 12
