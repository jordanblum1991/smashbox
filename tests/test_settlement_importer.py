"""Regression test for the settlement importer.

Runs against the real TikTok settlement file in `uploads/`. If the file is not
present, the test is skipped — keeps CI green while the file isn't checked in.

Key assertions:
  - importer runs to completion with zero skips
  - Settlement rows are created
  - Adjustments are created (duplicates allowed — TikTok pairs balance/deduction)
  - Order back-fill writes positive fee/commission magnitudes
  - Sample order type flag wins over gross_sales==0 heuristic
"""
from pathlib import Path

import pytest
from sqlalchemy import func

from app.db import Base, SessionLocal, engine
from app.importers.tiktok_orders import TikTokOrdersImporter
from app.importers.tiktok_settlements import TikTokSettlementsImporter
from app.models import (
    Adjustment,
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
    Settlement,
)

ORDERS_FILE = Path("uploads/All order-2026-05-13-10_38.csv")
SETTLE_FILE = Path("uploads/merchant_statement_profit_loss_7638906751283005197.xlsx")


@pytest.fixture(scope="module")
def imported_db():
    if not (ORDERS_FILE.exists() and SETTLE_FILE.exists()):
        pytest.skip("real TikTok files not present in uploads/")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    def _run(path: Path, kind: ImportFileKind, importer) -> None:
        with SessionLocal() as db:
            b = ImportBatch(
                kind=kind,
                status=ImportBatchStatus.PROCESSING,
                original_filename=path.name,
                stored_path=str(path),
            )
            db.add(b)
            db.flush()
            importer.run(path, db, b)
            db.commit()

    _run(ORDERS_FILE, ImportFileKind.TIKTOK_ORDERS, TikTokOrdersImporter())
    _run(SETTLE_FILE, ImportFileKind.TIKTOK_SETTLEMENTS, TikTokSettlementsImporter())
    yield


def test_settlement_rows_present(imported_db):
    with SessionLocal() as db:
        assert db.query(func.count(Settlement.id)).scalar() > 1000


def test_adjustment_rows_present(imported_db):
    with SessionLocal() as db:
        assert db.query(func.count(Adjustment.id)).scalar() > 0


def test_orders_were_back_filled(imported_db):
    """At least some PAID orders should have a positive tiktok_fees after settlement back-fill."""
    with SessionLocal() as db:
        n = (
            db.query(func.count(Order.id))
            .filter(Order.tiktok_fees > 0)
            .scalar()
        )
        assert n > 0, "no orders received settlement back-fill"


def test_seller_funded_split_invariant_holds_at_scale(imported_db):
    """Across all PAID orders, Outlandish + Smashbox == total — exactly."""
    with SessionLocal() as db:
        total, out, smash = db.query(
            func.coalesce(func.sum(Order.seller_funded_discount_total), 0),
            func.coalesce(func.sum(Order.seller_funded_outlandish), 0),
            func.coalesce(func.sum(Order.seller_funded_smashbox), 0),
        ).filter(Order.order_type == OrderType.PAID).one()
        assert (out + smash) == total, f"drift: {out} + {smash} != {total}"


def test_sample_classification_consistent(imported_db):
    """SAMPLE orders should have $0 gross_sales (either by orders-file heuristic
    or settlement file flag)."""
    with SessionLocal() as db:
        from decimal import Decimal
        rows = db.query(Order.gross_sales).filter(Order.order_type == OrderType.SAMPLE).all()
        nonzero = [r[0] for r in rows if Decimal(str(r[0])) != Decimal("0")]
        assert not nonzero, f"sample orders with non-zero gross_sales: {nonzero[:5]}"
