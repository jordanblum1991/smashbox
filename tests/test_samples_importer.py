"""Samples importer tests.

Unlike the TikTok-export importers, this one consumes a hand-edited template,
so the tests generate fixture files in a tmp dir rather than depending on
anything in uploads/.
"""
from datetime import datetime
from pathlib import Path

import pytest

from app.db import Base, SessionLocal, engine
from app.importers.samples import SamplesImporter
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Sample


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _run(path: Path):
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.SAMPLES,
            status=ImportBatchStatus.PROCESSING,
            original_filename=path.name,
            stored_path=str(path),
        )
        db.add(b)
        db.flush()
        result = SamplesImporter().run(path, db, b)
        db.commit()
        return result


def test_happy_path_csv(tmp_path):
    csv = tmp_path / "samples.csv"
    csv.write_text(
        "shipped_at,sku,quantity,creator_handle,is_paid_oversample,note\n"
        "2026-05-01,SBX-A,2,@alice,false,seed\n"
        "2026-05-02,1729873456789,1,Bob,true,oversample\n",
        encoding="utf-8",
    )

    result = _run(csv)
    assert result.rows_imported == 2
    assert result.rows_skipped == 0

    with SessionLocal() as db:
        rows = db.query(Sample).order_by(Sample.shipped_at).all()
        assert [r.sku for r in rows] == ["SBX-A", "1729873456789"]
        assert [r.quantity for r in rows] == [2, 1]
        assert [r.is_paid_oversample for r in rows] == [False, True]
        assert rows[0].shipped_at == datetime(2026, 5, 1)
        assert rows[1].creator_handle == "Bob"


def test_column_aliases_and_case_insensitive(tmp_path):
    """`Shipped At`, `SKU ID`, `Qty`, `Creator`, `Paid`, `Notes` should all map."""
    csv = tmp_path / "samples.csv"
    csv.write_text(
        "Shipped At,SKU ID,Qty,Creator,Paid,Notes\n"
        "05/01/2026,SBX-B,3,@charlie,yes,\n",
        encoding="utf-8",
    )

    result = _run(csv)
    assert result.rows_imported == 1
    with SessionLocal() as db:
        r = db.query(Sample).one()
        assert r.sku == "SBX-B"
        assert r.quantity == 3
        assert r.is_paid_oversample is True


def test_missing_required_column_raises(tmp_path):
    csv = tmp_path / "samples.csv"
    csv.write_text("sku,quantity\nSBX-A,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="shipped_at"):
        _run(csv)


def test_bad_rows_are_skipped_with_reasons(tmp_path):
    csv = tmp_path / "samples.csv"
    csv.write_text(
        "shipped_at,sku,quantity\n"
        "2026-05-01,SBX-A,1\n"
        ",SBX-B,1\n"                # missing shipped_at
        "2026-05-02,,1\n"           # missing sku
        "2026-05-03,SBX-C,-2\n"     # non-positive qty
        "2026-05-04,SBX-D,abc\n",   # bad qty
        encoding="utf-8",
    )
    result = _run(csv)
    assert result.rows_imported == 1
    assert result.rows_skipped == 4
    # Skip reasons reference the spreadsheet row number (header counted).
    assert any("row 3" in e for e in result.errors)
    assert any("row 4" in e for e in result.errors)


def test_xlsx_format_works(tmp_path):
    """Importer should accept .xlsx for users who prefer Excel."""
    import pandas as pd
    xlsx = tmp_path / "samples.xlsx"
    pd.DataFrame(
        [{"shipped_at": "2026-05-01", "sku": "SBX-A", "quantity": 1}]
    ).to_excel(xlsx, index=False)

    result = _run(xlsx)
    assert result.rows_imported == 1


def test_reupload_is_additive(tmp_path):
    """Re-uploading the same file appends — no natural-key dedup on a manual log."""
    csv = tmp_path / "samples.csv"
    csv.write_text(
        "shipped_at,sku,quantity\n2026-05-01,SBX-A,1\n",
        encoding="utf-8",
    )
    _run(csv)
    _run(csv)
    with SessionLocal() as db:
        assert db.query(Sample).count() == 2
