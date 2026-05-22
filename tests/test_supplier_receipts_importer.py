"""Supplier-receipt importer tests.

Covers the importer contract plus the two sample-module-specific invariants:
the alias map canonicalizes the sku column on insert, and batch rollback for
a receipt batch ONLY deletes that batch's IN rows — shipment OUT rows are
untouched (this is the load-bearing scoping guarantee).
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, SessionLocal, engine
from app.importers.supplier_receipts import SupplierReceiptImporter
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    SampleInventoryMovement,
    SampleMovementType,
    Sku,
    SkuAlias,
)
from app.services.batch_deletion import delete_batch
from app.services.sample_service import record_sample_shipment


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _make_batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.SUPPLIER_RECEIPTS,
        status=ImportBatchStatus.PROCESSING,
        original_filename="receipts.csv",
        stored_path="/tmp/receipts.csv",
    )
    db.add(b)
    db.flush()
    return b


def _run(path: Path) -> tuple:
    """Run the importer end-to-end, returning (ImportResult, batch_id)."""
    with SessionLocal() as db:
        b = _make_batch(db)
        result = SupplierReceiptImporter().run(path, db, b)
        db.commit()
        return result, b.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_csv(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date,unit_cost,po_number\n"
        "SBX-A,10,10,2026-05-01,1.50,PO-100\n"
        "SBX-B,5,5,2026-05-02,,PO-101\n",
        encoding="utf-8",
    )

    result, _ = _run(csv)
    assert result.rows_imported == 2
    assert result.rows_skipped == 0

    with SessionLocal() as db:
        rows = (
            db.query(SampleInventoryMovement)
            .order_by(SampleInventoryMovement.moved_at)
            .all()
        )
        assert [r.sku for r in rows] == ["SBX-A", "SBX-B"]
        assert [r.quantity for r in rows] == [10, 5]
        assert all(r.movement_type == SampleMovementType.IN for r in rows)
        assert rows[0].unit_cost == Decimal("1.50")
        assert rows[1].unit_cost is None
        assert rows[0].note == "PO PO-100"
        assert rows[1].note == "PO PO-101"
        assert rows[0].moved_at == datetime(2026, 5, 1)


# ---------------------------------------------------------------------------
# expected_quantity tolerance — received is the source of truth for stock
# ---------------------------------------------------------------------------

def test_discrepancy_noted_received_quantity_is_imported(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "SBX-A,30,28,2026-05-01\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1

    with SessionLocal() as db:
        m = db.query(SampleInventoryMovement).one()
        assert m.quantity == 28
        assert m.note == "expected 30, received 28"


def test_expected_blank_imports_received_without_note(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "SBX-A,,12,2026-05-01\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1
    with SessionLocal() as db:
        m = db.query(SampleInventoryMovement).one()
        assert m.quantity == 12
        assert m.note is None


def test_expected_garbled_imports_received_with_unreadable_note(tmp_path):
    """Unparseable or negative expected → import succeeds; the parse failure is
    surfaced in the movement's note so the data quality issue is auditable.

    Note: pandas' default `read_csv` treats common missing-value aliases like
    "N/A", "NULL", "NaN" as NaN — those are indistinguishable from a blank cell
    by design (and end up in the blank branch). The "unreadable" branch covers
    values that are genuinely non-numeric junk, e.g. "garbage", or negative
    numbers like "-3"."""
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date,po_number\n"
        "SBX-A,garbage,12,2026-05-01,PO-X\n"
        "SBX-B,-3,5,2026-05-02,\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 2
    assert result.rows_skipped == 0

    with SessionLocal() as db:
        rows = (
            db.query(SampleInventoryMovement)
            .order_by(SampleInventoryMovement.moved_at)
            .all()
        )
        assert rows[0].quantity == 12
        assert rows[0].note == "PO PO-X; expected: unreadable"
        assert rows[1].quantity == 5
        assert rows[1].note == "expected: unreadable"


def test_expected_column_missing_entirely_still_imports(tmp_path):
    """expected_quantity is column-level optional, not just per-row optional."""
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,received_quantity,received_date\n"
        "SBX-A,7,2026-05-01\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1
    with SessionLocal() as db:
        m = db.query(SampleInventoryMovement).one()
        assert m.quantity == 7
        assert m.note is None


# ---------------------------------------------------------------------------
# Real-field failures DO drop the row
# ---------------------------------------------------------------------------

def test_real_field_problems_skipped_with_reasons(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date,unit_cost\n"
        "SBX-A,10,10,2026-05-01,\n"          # row 2 — clean
        ",10,10,2026-05-01,\n"               # row 3 — missing sku
        "SBX-C,10,abc,2026-05-01,\n"         # row 4 — non-numeric received
        "SBX-D,10,-2,2026-05-01,\n"          # row 5 — non-positive received
        "SBX-E,10,10,not-a-date,\n"          # row 6 — unparseable date
        "SBX-F,10,10,2026-05-01,TBD\n"       # row 7 — bad unit_cost
        "SBX-G,10,,2026-05-01,\n",           # row 8 — missing received
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1
    assert result.rows_skipped == 6

    reasons = "\n".join(result.errors)
    for tag in ("row 3", "row 4", "row 5", "row 6", "row 7", "row 8"):
        assert tag in reasons, f"missing skip reason for {tag}: {reasons}"
    assert "missing sku" in reasons
    assert "bad received_quantity" in reasons
    assert "non-positive received_quantity" in reasons
    assert "unparseable received_date" in reasons
    assert "bad unit_cost" in reasons
    assert "missing received_quantity" in reasons


def test_missing_required_column_raises(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text("sku,received_quantity\nSBX-A,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="received_date"):
        _run(csv)


def test_column_aliases_and_case_insensitive(tmp_path):
    """`SKU`, `Expected`, `Received`, `Date`, `Cost`, `PO` should all map."""
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "SKU,Expected,Received,Date,Cost,PO\n"
        "SBX-A,4,4,2026-05-01,2.00,XYZ\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1
    with SessionLocal() as db:
        m = db.query(SampleInventoryMovement).one()
        assert m.sku == "SBX-A"
        assert m.quantity == 4
        assert m.unit_cost == Decimal("2.00")
        assert m.note == "PO XYZ"


# ---------------------------------------------------------------------------
# Sample-module invariants
# ---------------------------------------------------------------------------

def test_alias_canonicalization_on_sku(tmp_path):
    """A receipt under a legacy SKU code must land on the canonical SKU
    so its history doesn't split from the canonical balance."""
    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku="OLD-SKU", canonical_sku="SBX-NEW"))
        db.commit()

    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "OLD-SKU,5,5,2026-05-01\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 1

    with SessionLocal() as db:
        m = db.query(SampleInventoryMovement).one()
        assert m.sku == "SBX-NEW"


def test_brand_resolves_from_catalog_otherwise_unknown(tmp_path):
    """Brand pulled from Sku.brand when the catalog knows the SKU; falls back
    to "unknown" (matching the sentinel used in sku_master, bundle_mapping,
    and Creator.platform)."""
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-CATALOGUED", name="cat", brand="smashbox"))
        db.commit()

    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "SBX-CATALOGUED,1,1,2026-05-01\n"
        "SBX-NOTINCATALOG,1,1,2026-05-02\n",
        encoding="utf-8",
    )
    result, _ = _run(csv)
    assert result.rows_imported == 2

    with SessionLocal() as db:
        rows = (
            db.query(SampleInventoryMovement)
            .order_by(SampleInventoryMovement.moved_at)
            .all()
        )
        assert rows[0].brand == "smashbox"
        assert rows[1].brand == "unknown"


def test_rollback_deletes_receipt_in_rows_only_not_shipment_outs(tmp_path):
    """Critical scoping invariant.

    1. Record a shipment → produces an OUT row with NO import_batch_id.
    2. Run the receipt importer → produces IN rows tagged with the receipt batch.
    3. Roll back the receipt batch.
    4. IN rows from the receipt batch must be gone; the shipment OUT row
       must still exist untouched.
    """
    # 1. Pre-existing shipment (independent batch / ledger OUT).
    with SessionLocal() as db:
        sample_batch = ImportBatch(
            kind=ImportFileKind.SAMPLES,
            status=ImportBatchStatus.COMPLETED,
            original_filename="ship.csv",
            stored_path="/tmp/ship.csv",
        )
        db.add(sample_batch)
        db.flush()
        record_sample_shipment(
            db,
            sku="SBX-Z",
            quantity=2,
            shipped_at=datetime(2026, 5, 1),
            brand="smashbox",
            import_batch_id=sample_batch.id,
            creator_handle="@tester",
        )
        db.commit()

    with SessionLocal() as db:
        # Pre-state: exactly one OUT row, zero IN rows.
        assert (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.OUT)
            .count()
            == 1
        )
        assert (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.IN)
            .count()
            == 0
        )

    # 2. Run the receipts importer for a separate batch.
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "SBX-Z,5,5,2026-05-02\n"
        "SBX-Q,3,3,2026-05-02\n",
        encoding="utf-8",
    )
    _, receipt_batch_id = _run(csv)

    with SessionLocal() as db:
        assert (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.IN)
            .count()
            == 2
        )
        assert (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.OUT)
            .count()
            == 1
        )

    # 3. Roll back the receipt batch.
    with SessionLocal() as db:
        batch = db.get(ImportBatch, receipt_batch_id)
        deletion = delete_batch(db, batch)
        db.commit()
        assert deletion.kind == ImportFileKind.SUPPLIER_RECEIPTS
        assert deletion.rows_deleted == 2

    # 4. IN rows from this batch are gone; the shipment OUT row survives.
    with SessionLocal() as db:
        assert (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.IN)
            .count()
            == 0
        )
        survivors = (
            db.query(SampleInventoryMovement)
            .filter_by(movement_type=SampleMovementType.OUT)
            .all()
        )
        assert len(survivors) == 1
        assert survivors[0].sku == "SBX-Z"
        assert survivors[0].quantity == 2


# ---------------------------------------------------------------------------
# Re-upload behaviour matches SamplesImporter (additive, no natural-key dedup)
# ---------------------------------------------------------------------------

def test_reupload_is_additive(tmp_path):
    csv = tmp_path / "receipts.csv"
    csv.write_text(
        "sku,expected_quantity,received_quantity,received_date\n"
        "SBX-A,3,3,2026-05-01\n",
        encoding="utf-8",
    )
    _run(csv)
    _run(csv)
    with SessionLocal() as db:
        assert db.query(SampleInventoryMovement).count() == 2
