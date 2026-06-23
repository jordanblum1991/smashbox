"""Inventory valuation rule: sample inventory carries $0 COGS, so it contributes
nothing to inventory value. Sample UNITS are still counted (physical on-hand);
only their VALUE is excluded. Sellable inventory is valued at unit COGS."""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.reports.inventory_report import compute_inventory_report


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                    status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    return b


def test_sample_inventory_excluded_from_value():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", name="Primer", brand="smashbox",
                   tiktok_sku_id="111", unit_cogs=Decimal("3.00")))
        b = _batch(db)
        when = datetime(2026, 6, 23, 7, 0)
        db.add(InventorySnapshot(import_batch_id=b.id, sku="SBX-A",
                                 on_hand=10, captured_at=when))        # sellable
        db.add(SampleInventorySnapshot(import_batch_id=b.id, sku="SBX-A",
                                       on_hand=5, captured_at=when))   # sample
        db.commit()
        view = compute_inventory_report(db)

    row = next(r for r in view.rows if r.sku_code == "SBX-A")
    # Units: samples still counted physically.
    assert row.sellable_on_hand == 10
    assert row.sample_on_hand == 5
    assert row.total_on_hand == 15
    # Value: sellable at COGS; sample at $0; total excludes sample.
    assert row.sellable_value == Decimal("30.00")
    assert row.sample_value == Decimal("0")
    assert row.total_value == Decimal("30.00")
    # Roll-ups.
    assert view.total_sellable_value == Decimal("30.00")
    assert view.total_sample_value == Decimal("0")
    assert view.total_inventory_value == Decimal("30.00")
