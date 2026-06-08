"""Sample classification is reconciled from settlement, order-independent.

Root cause of a real GMV discrepancy: `order_type` was written by two importers
(orders-file gross heuristic + settlement `Sample order type`) with no
reconciliation when the orders file loaded AFTER its settlement. 12 "free sample
from Tiktok Shop" orders (gross $46 each) stayed PAID and inflated GMV by $552.

The fix: a single `reconcile_sample_classifications` pass — the authoritative
place that applies the settlement sample-type rule to every order, idempotently,
regardless of import order.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Settlement
from app.models.order import Order, OrderType
from app.services.sample_classification import (
    classify_from_sample_order_type,
    reconcile_sample_classifications,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db, kind=ImportFileKind.TIKTOK_ORDERS) -> ImportBatch:
    b = ImportBatch(kind=kind, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b)
    db.flush()
    return b


def _order(db, bid, oid, *, order_type=OrderType.PAID, gross=Decimal("46.00")) -> Order:
    o = Order(import_batch_id=bid, tiktok_order_id=oid, placed_at=datetime(2026, 5, 10),
              order_type=order_type, status="Shipped", brand="smashbox", gross_sales=gross)
    db.add(o)
    db.flush()
    return o


def _settlement(db, bid, oid, sot, *, stmt="S1", paid=datetime(2026, 5, 11)):
    db.add(Settlement(import_batch_id=bid, tiktok_order_id=oid, linked_statement_id=stmt,
                      sample_order_type=sot, paid_date=paid))
    db.flush()


# --- classifier ------------------------------------------------------------

@pytest.mark.parametrize("sot,expected", [
    ("free sample from Tiktok Shop", OrderType.SAMPLE),
    ("free sample from seller", OrderType.SAMPLE),
    ("paid sample", OrderType.PAID_SAMPLE),
    ("oversample", OrderType.PAID_SAMPLE),
    ("", None),
    (None, None),
    ("regular order", None),
])
def test_classify_from_sample_order_type(sot, expected):
    assert classify_from_sample_order_type(sot) == expected


# --- reconcile -------------------------------------------------------------

def test_reconcile_promotes_free_sample_to_sample():
    """The bug: a PAID order whose settlement says free-sample becomes SAMPLE."""
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "O1", order_type=OrderType.PAID)
        _settlement(db, sb.id, "O1", "free sample from Tiktok Shop")
        db.commit()
        changed = reconcile_sample_classifications(db)
        db.commit()
        assert changed == 1
        assert db.query(Order).filter_by(tiktok_order_id="O1").one().order_type == OrderType.SAMPLE


def test_reconcile_promotes_paid_sample():
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "O1", order_type=OrderType.PAID)
        _settlement(db, sb.id, "O1", "paid sample")
        db.commit()
        reconcile_sample_classifications(db)
        db.commit()
        assert db.query(Order).filter_by(tiktok_order_id="O1").one().order_type == OrderType.PAID_SAMPLE


def test_reconcile_leaves_non_sample_orders_alone():
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "PAID", order_type=OrderType.PAID)
        _settlement(db, sb.id, "PAID", "")           # no sample flag
        _order(db, ob.id, "NOSETTLE", order_type=OrderType.PAID)  # no settlement at all
        db.commit()
        changed = reconcile_sample_classifications(db)
        db.commit()
        assert changed == 0
        assert db.query(Order).filter_by(tiktok_order_id="PAID").one().order_type == OrderType.PAID
        assert db.query(Order).filter_by(tiktok_order_id="NOSETTLE").one().order_type == OrderType.PAID


def test_reconcile_demotes_heuristic_sample_when_settlement_says_not_sample():
    """A $0 order classified SAMPLE by the gross heuristic, but whose settlement
    carries no sample flag, is demoted to PAID — settlement is authoritative and
    overrides the $0 heuristic (TikTok counts it as an order)."""
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "O1", order_type=OrderType.SAMPLE, gross=Decimal("0.00"))
        _settlement(db, sb.id, "O1", "")          # settlement present, NOT flagged a sample
        db.commit()
        changed = reconcile_sample_classifications(db)
        db.commit()
        assert changed == 1
        assert db.query(Order).filter_by(tiktok_order_id="O1").one().order_type == OrderType.PAID


def test_reconcile_does_not_demote_sample_without_settlement():
    """A SAMPLE order with NO settlement keeps the $0 heuristic — there's no
    authoritative settlement to override it (off-platform / unsettled samples)."""
    with SessionLocal() as db:
        ob = _batch(db)
        _order(db, ob.id, "O1", order_type=OrderType.SAMPLE, gross=Decimal("0.00"))
        db.commit()
        changed = reconcile_sample_classifications(db)
        db.commit()
        assert changed == 0
        assert db.query(Order).filter_by(tiktok_order_id="O1").one().order_type == OrderType.SAMPLE


def test_reconcile_uses_latest_settlement():
    """When an order has multiple settlements, the latest (by paid date) wins."""
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "O1", order_type=OrderType.PAID)
        _settlement(db, sb.id, "O1", "", stmt="early", paid=datetime(2026, 5, 1))
        _settlement(db, sb.id, "O1", "free sample from Tiktok Shop", stmt="late", paid=datetime(2026, 5, 20))
        db.commit()
        reconcile_sample_classifications(db)
        db.commit()
        assert db.query(Order).filter_by(tiktok_order_id="O1").one().order_type == OrderType.SAMPLE


def test_reconcile_is_idempotent():
    with SessionLocal() as db:
        ob, sb = _batch(db), _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)
        _order(db, ob.id, "O1", order_type=OrderType.PAID)
        _settlement(db, sb.id, "O1", "free sample from Tiktok Shop")
        db.commit()
        assert reconcile_sample_classifications(db) == 1
        db.commit()
        assert reconcile_sample_classifications(db) == 0  # nothing left to change
