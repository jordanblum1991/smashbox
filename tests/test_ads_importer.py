"""Regression test for the TikTok Ads "Cost" export importer.

Runs against the real Cost workbook in `uploads/` if present; otherwise
skips so CI stays green.

Key assertions:
  - importer runs to completion
  - one AdSpend per (date, campaign_id) — the trailing "Total" row is skipped
  - amounts are stored as POSITIVE magnitudes (TikTok writes them negative)
  - sum across the imported window matches |sum of Amount column|
  - re-importing is idempotent (row count + total unchanged)
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func

from app.db import Base, SessionLocal, engine
from app.importers.tiktok_ads import TikTokAdsImporter
from app.models import AdSpend, ImportBatch, ImportBatchStatus, ImportFileKind

ADS_FILES = list(Path("uploads").glob("Cost_*.xlsx"))


def _run_import(path: Path) -> None:
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.TIKTOK_ADS,
            status=ImportBatchStatus.PROCESSING,
            original_filename=path.name,
            stored_path=str(path),
        )
        db.add(b)
        db.flush()
        TikTokAdsImporter().run(path, db, b)
        db.commit()


@pytest.fixture(scope="module")
def imported_db():
    if not ADS_FILES:
        pytest.skip("no Cost_*.xlsx file present in uploads/")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _run_import(ADS_FILES[0])
    yield ADS_FILES[0]


def test_total_row_is_skipped(imported_db):
    """The Cost export sometimes has a footer Total row whose Date == 'Total'.
    The importer must skip it — DB count equals data-row count in the file."""
    df = pd.read_excel(imported_db, dtype=str)
    # Real data rows = rows where Date parses (excludes "Total" + blanks).
    expected = df["Date"].apply(
        lambda v: pd.notna(v) and str(v).strip().lower() != "total" and str(v).strip() != ""
    ).sum()

    with SessionLocal() as db:
        assert db.query(func.count(AdSpend.id)).scalar() == expected


def test_amounts_stored_as_positive(imported_db):
    """TikTok writes ad costs as NEGATIVE numbers. We store the magnitude so
    the P&L can subtract directly."""
    with SessionLocal() as db:
        rows = db.query(AdSpend).all()
        assert rows, "no ad spend rows imported"
        for r in rows:
            assert r.amount >= 0, f"{r.spend_date.date()} {r.campaign_id} stored as negative"


def test_total_matches_file(imported_db):
    """SUM(AdSpend.amount) == |SUM(file.Amount)| (excluding the Total row)."""
    df = pd.read_excel(imported_db)
    data_rows = df[df["Date"].apply(
        lambda v: pd.notna(v) and str(v).strip().lower() != "total"
    )]
    expected = abs(Decimal(str(data_rows["Amount"].sum())))

    with SessionLocal() as db:
        actual = Decimal(str(
            db.query(func.coalesce(func.sum(AdSpend.amount), 0)).scalar()
        ))
    assert actual == expected, f"total drift: {actual} vs {expected}"


def test_reimport_is_idempotent(imported_db):
    """Re-running on the same file must not duplicate rows or shift totals."""
    with SessionLocal() as db:
        before_count = db.query(func.count(AdSpend.id)).scalar()
        before_sum = Decimal(str(
            db.query(func.coalesce(func.sum(AdSpend.amount), 0)).scalar()
        ))

    _run_import(imported_db)

    with SessionLocal() as db:
        after_count = db.query(func.count(AdSpend.id)).scalar()
        after_sum = Decimal(str(
            db.query(func.coalesce(func.sum(AdSpend.amount), 0)).scalar()
        ))

    assert after_count == before_count
    assert after_sum == before_sum
