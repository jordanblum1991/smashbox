"""InventorySnapshotImporter — file format + idempotency."""
from datetime import datetime
from pathlib import Path

import pytest

from app.db import Base, SessionLocal, engine
from app.importers.inventory_snapshot import InventorySnapshotImporter
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, InventorySnapshot


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _run(path: Path):
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.INVENTORY_SNAPSHOT,
            status=ImportBatchStatus.PROCESSING,
            original_filename=path.name,
            stored_path=str(path),
        )
        db.add(b)
        db.flush()
        result = InventorySnapshotImporter().run(path, db, b)
        db.commit()
        return result


def test_happy_path_csv(tmp_path):
    csv = tmp_path / "inventory.csv"
    csv.write_text(
        "sku,on_hand,captured_at\n"
        "SBX-A,250,2026-05-19\n"
        "1729873456789,42,2026-05-19\n"
        "SBX-B,0,2026-05-19\n",
        encoding="utf-8",
    )

    result = _run(csv)
    assert result.rows_imported == 3
    assert result.rows_skipped == 0

    with SessionLocal() as db:
        rows = db.query(InventorySnapshot).order_by(InventorySnapshot.sku).all()
        assert [r.sku for r in rows] == ["1729873456789", "SBX-A", "SBX-B"]
        assert [r.on_hand for r in rows] == [42, 250, 0]
        assert all(r.captured_at == datetime(2026, 5, 19) for r in rows)


def test_missing_captured_at_defaults_to_upload_time(tmp_path):
    csv = tmp_path / "inventory.csv"
    csv.write_text("sku,on_hand\nSBX-A,100\n", encoding="utf-8")

    before = datetime.utcnow()
    result = _run(csv)
    after = datetime.utcnow()

    assert result.rows_imported == 1
    with SessionLocal() as db:
        row = db.query(InventorySnapshot).one()
        assert row.sku == "SBX-A"
        assert row.on_hand == 100
        assert before <= row.captured_at <= after


def test_idempotent_reupload_updates_in_place(tmp_path):
    """Re-uploading the same (sku, captured_at) updates on_hand rather than
    appending a duplicate row."""
    csv_v1 = tmp_path / "v1.csv"
    csv_v1.write_text(
        "sku,on_hand,captured_at\nSBX-A,250,2026-05-19\n", encoding="utf-8"
    )
    csv_v2 = tmp_path / "v2.csv"
    csv_v2.write_text(
        "sku,on_hand,captured_at\nSBX-A,300,2026-05-19\n", encoding="utf-8"
    )

    _run(csv_v1)
    _run(csv_v2)

    with SessionLocal() as db:
        rows = db.query(InventorySnapshot).filter(InventorySnapshot.sku == "SBX-A").all()
        assert len(rows) == 1
        assert rows[0].on_hand == 300


def test_different_captured_at_creates_new_row(tmp_path):
    """Two snapshots of the same SKU on different dates are both retained."""
    csv = tmp_path / "history.csv"
    csv.write_text(
        "sku,on_hand,captured_at\n"
        "SBX-A,250,2026-05-12\n"
        "SBX-A,180,2026-05-19\n",
        encoding="utf-8",
    )
    _run(csv)

    with SessionLocal() as db:
        rows = (
            db.query(InventorySnapshot)
            .filter(InventorySnapshot.sku == "SBX-A")
            .order_by(InventorySnapshot.captured_at)
            .all()
        )
        assert [r.on_hand for r in rows] == [250, 180]
        assert rows[0].captured_at == datetime(2026, 5, 12)
        assert rows[1].captured_at == datetime(2026, 5, 19)


def test_bad_rows_are_skipped_with_reasons(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "sku,on_hand,captured_at\n"
        ",100,2026-05-19\n"               # missing sku
        "SBX-A,abc,2026-05-19\n"         # bad on_hand
        "SBX-B,-5,2026-05-19\n"          # negative on_hand
        "SBX-C,42,2026-05-19\n",         # good
        encoding="utf-8",
    )
    result = _run(csv)
    assert result.rows_imported == 1
    assert result.rows_skipped == 3
    assert len(result.errors) == 3
    # Reasons should mention what was bad on each row
    joined = "\n".join(result.errors).lower()
    assert "sku" in joined
    assert "on_hand" in joined or "0" not in joined  # specific message phrasing


def test_column_aliases_tolerated(tmp_path):
    """Operators paste from various sources — accept common synonyms."""
    csv = tmp_path / "aliased.csv"
    csv.write_text(
        "SKU,Quantity,As Of\n"
        "SBX-A,42,2026-05-19\n",
        encoding="utf-8",
    )
    result = _run(csv)
    assert result.rows_imported == 1
    with SessionLocal() as db:
        row = db.query(InventorySnapshot).one()
        assert row.sku == "SBX-A"
        assert row.on_hand == 42
        assert row.captured_at == datetime(2026, 5, 19)
