"""Order-coverage gap detection."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Order, OrderType
from app.reports.coverage_gaps import compute_order_coverage


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _order(db, oid, day: datetime):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="x", stored_path="")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=oid, placed_at=day,
                 order_type=OrderType.PAID, status="Completed", brand="smashbox",
                 gross_sales=Decimal("10")))


def test_detects_single_day_gap():
    with SessionLocal() as db:
        _order(db, "A", datetime(2026, 5, 1, 9))
        _order(db, "B", datetime(2026, 5, 3, 9))  # May 2 missing
        db.commit()
    with SessionLocal() as db:
        cov = compute_order_coverage(db)
        assert cov.first_day == date(2026, 5, 1) and cov.last_day == date(2026, 5, 3)
        assert cov.missing_days == 1 and len(cov.gaps) == 1
        assert cov.gaps[0].start == date(2026, 5, 2) and cov.gaps[0].days == 1


def test_no_gap_when_contiguous():
    with SessionLocal() as db:
        _order(db, "A", datetime(2026, 5, 1, 9))
        _order(db, "B", datetime(2026, 5, 2, 9))
        db.commit()
    with SessionLocal() as db:
        cov = compute_order_coverage(db)
        assert cov.gaps == [] and cov.missing_days == 0 and cov.covered_days == 2


def test_empty_db():
    with SessionLocal() as db:
        cov = compute_order_coverage(db)
        assert cov.first_day is None and cov.gaps == []
