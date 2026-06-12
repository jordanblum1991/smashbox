"""The in-memory ingestion seam for orders — what a TikTok API client will use.

Feeds a DataFrame (columns named per the TikTok export headers, as the API client
would map order JSON) straight into `import_dataframe`, bypassing any file, and
checks it produces the same Orders + the seller-funded split, and that re-ingesting
the same data is idempotent (the scheduled-API-re-pull case).
"""
from decimal import Decimal

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers.tiktok_orders import import_dataframe
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Order, OrderType


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.PROCESSING,
                    original_filename="api-pull", stored_path="")
    db.add(b)
    db.flush()
    return b


def _df(order_id="ORD-1", gross="100", seller_disc="24", platform_disc="20"):
    """One order line, columns named exactly like the TikTok export (= the seam's
    contract). The canonical worked example: $100 gross, $20 TikTok-funded, $24
    seller-funded → Outlandish $8, Smashbox $16."""
    return pd.DataFrame([{
        "Order ID": order_id,
        "Order Status": "Completed",
        "Created Time": "2026-05-13 10:00:00",
        "SKU ID": "1729000000000000001",
        "Quantity": "1",
        "SKU Unit Original Price": "100",
        "SKU Subtotal Before Discount": gross,
        "SKU Seller Discount": seller_disc,
        "SKU Platform Discount": platform_disc,
    }])


def test_in_memory_records_create_order_with_split():
    with SessionLocal() as db:
        res = import_dataframe(_df(), db, _batch(db))
        db.commit()
        assert res.rows_imported == 1
    with SessionLocal() as db:
        o = db.query(Order).one()
        assert o.tiktok_order_id == "ORD-1"
        assert o.gross_sales == Decimal("100")
        assert o.seller_funded_discount_total == Decimal("24")
        assert o.seller_funded_outlandish == Decimal("8.00")   # MIN(24, 80 × 10%)
        assert o.seller_funded_smashbox == Decimal("16.00")    # residual
        assert o.order_type == OrderType.PAID


def test_reingest_same_order_is_idempotent():
    with SessionLocal() as db:
        import_dataframe(_df(), db, _batch(db))
        db.commit()
    with SessionLocal() as db:  # simulate a scheduled API re-pull of the same order
        import_dataframe(_df(), db, _batch(db))
        db.commit()
    with SessionLocal() as db:
        assert db.query(Order).count() == 1  # upserted on tiktok_order_id, not duplicated


def test_zero_gross_classified_as_sample():
    with SessionLocal() as db:
        import_dataframe(_df(order_id="ORD-S", gross="0", seller_disc="0", platform_disc="0"),
                         db, _batch(db))
        db.commit()
    with SessionLocal() as db:
        o = db.query(Order).filter_by(tiktok_order_id="ORD-S").one()
        assert o.order_type == OrderType.SAMPLE
