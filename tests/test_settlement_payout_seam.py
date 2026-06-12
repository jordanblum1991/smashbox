"""In-memory ingestion seams for settlements + payouts — what the TikTok API
client will call. Feeds constructed DataFrames (columns named per the export
headers) straight into import_dataframes, no file, and checks creation +
idempotent re-ingest (the scheduled-re-pull case)."""
from decimal import Decimal

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers import tiktok_payouts as tp
from app.importers import tiktok_settlements as ts
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Payout, Settlement


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db, kind):
    b = ImportBatch(kind=kind, status=ImportBatchStatus.PROCESSING,
                    original_filename="api-pull", stored_path="")
    db.add(b)
    db.flush()
    return b


# ----- settlements (Orders-sheet frame + optional Adjustment frame) -----------

def _settle_df():
    return pd.DataFrame([{
        "Order ID": "S-1",
        "linked statement id": "STMT-1",
        "Order status": "Settled",
        "Gross sales": "100.00",
        "Referral fee": "-5.00",
    }])


def test_settlement_seam_creates_and_is_idempotent():
    with SessionLocal() as db:
        res = ts.import_dataframes(_settle_df(), None, db, _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS))
        db.commit()
        assert res.rows_imported == 1
    with SessionLocal() as db:  # scheduled API re-pull of the same statement
        ts.import_dataframes(_settle_df(), None, db, _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS))
        db.commit()
    with SessionLocal() as db:
        rows = db.query(Settlement).all()
        assert len(rows) == 1  # upsert on (order, statement), not duplicated
        assert rows[0].tiktok_order_id == "S-1"


# ----- payouts (Payments-sheet frame + optional Statements frame) -------------

def _pay_df():
    return pd.DataFrame([{
        "Payment ID": "P-1", "Payment amount": "1000.00",
        "Payment completion date": "2026/05/01", "Status": "Paid",
    }])


def _stmt_df():
    return pd.DataFrame([{
        "Payment ID": "P-1", "Statement ID": "ST-1",
        "Statement date": "2026/04/15", "Net sales": "1100.00",
    }])


def test_payout_seam_creates_with_derived_fees_and_idempotent():
    with SessionLocal() as db:
        res = tp.import_dataframes(_pay_df(), _stmt_df(), db, _batch(db, ImportFileKind.TIKTOK_PAYOUTS))
        db.commit()
        assert res.rows_imported == 1
    with SessionLocal() as db:
        p = db.query(Payout).one()
        assert p.payout_id == "P-1"
        assert p.net_amount == Decimal("1000.00")    # cash side
        assert p.gross_amount == Decimal("1100.00")  # from Statements net sales
        assert p.fees == Decimal("100.00")           # gross − net
    with SessionLocal() as db:  # re-pull
        tp.import_dataframes(_pay_df(), _stmt_df(), db, _batch(db, ImportFileKind.TIKTOK_PAYOUTS))
        db.commit()
    with SessionLocal() as db:
        assert db.query(Payout).count() == 1  # upsert on payout_id


def test_payout_seam_without_statements():
    """No Statements frame → cash row still imports, gross defaults to net, fees 0."""
    with SessionLocal() as db:
        tp.import_dataframes(_pay_df(), None, db, _batch(db, ImportFileKind.TIKTOK_PAYOUTS))
        db.commit()
    with SessionLocal() as db:
        p = db.query(Payout).one()
        assert p.net_amount == Decimal("1000.00") and p.fees == Decimal("0")
