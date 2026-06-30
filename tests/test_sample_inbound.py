"""Inbound sample computation — open orders count as on-order sample stock,
replicated under every catalog identifier; received orders clear."""
from datetime import datetime

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.reports.sample_inbound import compute_sample_inbound, sample_inbound_summary
from app.reports.sample_inventory import compute_sample_inventory_view


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _order(db, status, lines):
    o = SampleInboundOrder(source="Acme", status=status)
    db.add(o); db.flush()
    for sku, qty in lines:
        db.add(SampleInboundOrderLine(sample_inbound_order_id=o.id, sku=sku, quantity=qty))
    return o


def test_open_orders_replicate_under_every_identifier():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-S1", name="Primer", brand="smashbox",
                   tiktok_sku_id="111", tiktok_alt_sku="C1"))
        _order(db, "open", [("SBX-S1", 10)])
        db.commit()
        inbound = compute_sample_inbound(db)
    # Same qty reachable via any of the SKU's identifiers (no double counting).
    assert inbound["SBX-S1"] == 10
    assert inbound["111"] == 10
    assert inbound["C1"] == 10


def test_received_orders_are_excluded():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-S1", name="Primer", brand="smashbox", tiktok_sku_id="111"))
        _order(db, "open", [("SBX-S1", 4)])
        _order(db, "received", [("SBX-S1", 99)])   # cleared — SAP on-hand owns it now
        db.commit()
        inbound = compute_sample_inbound(db)
    assert inbound["SBX-S1"] == 4


def test_empty_when_no_open_orders():
    with SessionLocal() as db:
        assert compute_sample_inbound(db) == {}


def test_report_folds_inbound_into_sample_view():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", name="Primer A", brand="smashbox", tiktok_sku_id="111"))
        db.add(Sku(sku="SBX-B", name="Primer B", brand="smashbox", tiktok_sku_id="222"))
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="t", stored_path="t")
        db.add(b); db.flush()
        db.add(SampleInventorySnapshot(import_batch_id=b.id, sku="SBX-A", on_hand=5,
                                       captured_at=datetime(2026, 6, 23, 7, 0)))
        _order(db, "open", [("SBX-A", 3), ("SBX-B", 7)])   # SBX-B is inbound-only
        db.commit()
        view = compute_sample_inventory_view(db)

    rows = {r.sku_code: r for r in view.rows}
    assert rows["SBX-A"].on_hand_units == 5
    assert rows["SBX-A"].inbound_units == 3
    assert rows["SBX-A"].total_units == 8
    # An inbound-only SKU (no SAP on-hand yet) still shows up.
    assert rows["SBX-B"].on_hand_units == 0
    assert rows["SBX-B"].inbound_units == 7
    assert rows["SBX-B"].total_units == 7
    assert view.total_inbound_units == 10
    assert view.total_units == 15


def test_summary_counts_orders_and_units_without_replication():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-S1", name="Primer", brand="smashbox", tiktok_sku_id="111"))
        _order(db, "open", [("SBX-S1", 10), ("SBX-S2", 5)])
        _order(db, "open", [("SBX-S1", 2)])
        _order(db, "received", [("SBX-S1", 100)])
        db.commit()
        s = sample_inbound_summary(db)
    assert s["open_orders"] == 2
    assert s["units_inbound"] == 17   # 10 + 5 + 2, no per-identifier replication
