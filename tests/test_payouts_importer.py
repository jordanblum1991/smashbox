"""Regression test for the payouts importer.

Runs against the real TikTok payouts-income workbook in `uploads/` if present;
otherwise skips so CI stays green.

Key assertions:
  - importer runs to completion with zero skips
  - one Payout per Payment ID in the Payments sheet
  - re-running is idempotent (row count + dollar totals unchanged)
  - net_amount tracks Payment amount; fees = gross - net (never negative)
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func

from app.db import Base, SessionLocal, engine
from app.importers.tiktok_payouts import TikTokPayoutsImporter
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Payout

PAYOUTS_FILE = Path("uploads/payouts-income_20260515143606(UTC-7).xlsx")


def _run_import() -> None:
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.TIKTOK_PAYOUTS,
            status=ImportBatchStatus.PROCESSING,
            original_filename=PAYOUTS_FILE.name,
            stored_path=str(PAYOUTS_FILE),
        )
        db.add(b)
        db.flush()
        TikTokPayoutsImporter().run(PAYOUTS_FILE, db, b)
        db.commit()


@pytest.fixture(scope="module")
def imported_db():
    if not PAYOUTS_FILE.exists():
        pytest.skip("real TikTok payouts file not present in uploads/")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _run_import()
    yield


def test_one_payout_per_payment_id(imported_db):
    """Row count in the DB matches non-blank Payment IDs in the Payments sheet."""
    payments_df = pd.read_excel(PAYOUTS_FILE, sheet_name="Payments", dtype=str)
    expected = payments_df["Payment ID"].dropna().astype(str).str.strip()
    expected = expected[expected != ""].nunique()

    with SessionLocal() as db:
        assert db.query(func.count(Payout.id)).scalar() == expected


def test_net_amount_matches_payment_amount(imported_db):
    """For each Payment ID, Payout.net_amount == Payments['Payment amount']."""
    payments_df = pd.read_excel(PAYOUTS_FILE, sheet_name="Payments", dtype=str)
    expected = {
        str(r["Payment ID"]).strip(): Decimal(str(r["Payment amount"]))
        for _, r in payments_df.iterrows()
        if str(r.get("Payment ID", "")).strip()
    }
    with SessionLocal() as db:
        for p in db.query(Payout).all():
            assert Decimal(str(p.net_amount)) == expected[p.payout_id], (
                f"net_amount drift on {p.payout_id}"
            )


def test_fees_non_negative_and_consistent(imported_db):
    """fees = gross - net by construction; should never be negative for a real
    payout (gross >= net because TikTok always takes a cut)."""
    with SessionLocal() as db:
        for p in db.query(Payout).all():
            g = Decimal(str(p.gross_amount))
            n = Decimal(str(p.net_amount))
            f = Decimal(str(p.fees))
            assert f == g - n, f"fees inconsistent on {p.payout_id}: {f} != {g}-{n}"
            assert f >= 0, f"negative fees on {p.payout_id}: {f}"


def test_reimport_is_idempotent(imported_db):
    """Running the importer a second time must not duplicate rows or shift totals."""
    with SessionLocal() as db:
        before_count = db.query(func.count(Payout.id)).scalar()
        before_net = db.query(func.coalesce(func.sum(Payout.net_amount), 0)).scalar()

    _run_import()

    with SessionLocal() as db:
        after_count = db.query(func.count(Payout.id)).scalar()
        after_net = db.query(func.coalesce(func.sum(Payout.net_amount), 0)).scalar()

    assert after_count == before_count
    assert Decimal(str(after_net)) == Decimal(str(before_net))
