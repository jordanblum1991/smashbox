"""Batch deletion service tests.

Covers the per-kind cascade rules in app/services/batch_deletion.py:
  - orders: cascade to lines
  - settlements: cascade to adjustments + zero/recompute Order back-fill
  - payouts / samples: plain row delete by batch id
  - catalog (sku_master, bundle_mapping): audit-only — catalog rows survive
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    Bundle,
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
    Payout,
    Sample,
    Settlement,
    Sku,
)
from app.services.batch_deletion import delete_batch


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _new_batch(db, kind: ImportFileKind, name: str = "f.csv") -> ImportBatch:
    b = ImportBatch(
        kind=kind,
        status=ImportBatchStatus.COMPLETED,
        original_filename=name,
        stored_path=f"/tmp/{name}",
    )
    db.add(b)
    db.flush()
    return b


def test_delete_orders_cascades_to_lines():
    with SessionLocal() as db:
        b = _new_batch(db, ImportFileKind.TIKTOK_ORDERS)
        order = Order(
            import_batch_id=b.id,
            tiktok_order_id="O-1",
            placed_at=datetime(2026, 5, 1),
            order_type=OrderType.PAID,
            status="Completed",
            brand="smashbox",
        )
        db.add(order)
        db.flush()
        db.add(OrderLine(order_id=order.id, sku="SBX-A", quantity=2))
        db.add(OrderLine(order_id=order.id, sku="SBX-B", quantity=1))
        db.commit()

        assert db.query(OrderLine).count() == 2

        delete_batch(db, db.get(ImportBatch, b.id))
        db.commit()

        assert db.query(Order).count() == 0
        assert db.query(OrderLine).count() == 0
        assert db.query(ImportBatch).count() == 0


def test_delete_settlement_zeros_backfill_when_no_remaining():
    with SessionLocal() as db:
        orders_b = _new_batch(db, ImportFileKind.TIKTOK_ORDERS, "orders.csv")
        order = Order(
            import_batch_id=orders_b.id,
            tiktok_order_id="O-1",
            placed_at=datetime(2026, 5, 1),
            order_type=OrderType.PAID,
            status="Completed",
            brand="smashbox",
            tiktok_fees=Decimal("10.00"),
            affiliate_commission=Decimal("3.00"),
            refunds=Decimal("5.00"),
        )
        db.add(order)
        db.flush()

        settle_b = _new_batch(db, ImportFileKind.TIKTOK_SETTLEMENTS, "settle.xlsx")
        db.add(Settlement(
            import_batch_id=settle_b.id,
            tiktok_order_id="O-1",
            linked_statement_id="S-1",
            tiktok_fees=Decimal("10.00"),
            affiliate_commission=Decimal("3.00"),
            gross_sales_refund=Decimal("5.00"),
        ))
        db.commit()

        delete_batch(db, db.get(ImportBatch, settle_b.id))
        db.commit()

        order = db.query(Order).one()
        assert Decimal(str(order.tiktok_fees)) == Decimal("0")
        assert Decimal(str(order.affiliate_commission)) == Decimal("0")
        assert Decimal(str(order.refunds)) == Decimal("0")
        assert db.query(Settlement).count() == 0


def test_delete_settlement_recomputes_from_remaining():
    """When a second settlement batch exists for the same order, deleting one
    should re-apply the other's values to the Order."""
    with SessionLocal() as db:
        orders_b = _new_batch(db, ImportFileKind.TIKTOK_ORDERS, "orders.csv")
        order = Order(
            import_batch_id=orders_b.id,
            tiktok_order_id="O-1",
            placed_at=datetime(2026, 5, 1),
            order_type=OrderType.PAID,
            status="Completed",
            brand="smashbox",
            tiktok_fees=Decimal("20.00"),  # value from the SECOND batch
        )
        db.add(order)
        db.flush()

        # First (older) settlement
        b1 = _new_batch(db, ImportFileKind.TIKTOK_SETTLEMENTS, "settle-1.xlsx")
        db.add(Settlement(
            import_batch_id=b1.id,
            tiktok_order_id="O-1",
            linked_statement_id="S-1",
            paid_date=datetime(2026, 5, 5),
            tiktok_fees=Decimal("10.00"),
        ))
        # Second (newer) settlement — values currently on the Order
        b2 = _new_batch(db, ImportFileKind.TIKTOK_SETTLEMENTS, "settle-2.xlsx")
        db.add(Settlement(
            import_batch_id=b2.id,
            tiktok_order_id="O-1",
            linked_statement_id="S-2",
            paid_date=datetime(2026, 5, 10),
            tiktok_fees=Decimal("20.00"),
        ))
        db.commit()

        # Delete the newer one — Order should fall back to the older settlement.
        delete_batch(db, db.get(ImportBatch, b2.id))
        db.commit()

        order = db.query(Order).one()
        assert Decimal(str(order.tiktok_fees)) == Decimal("10.00")


def test_delete_payouts_removes_only_this_batches_rows():
    with SessionLocal() as db:
        b1 = _new_batch(db, ImportFileKind.TIKTOK_PAYOUTS, "p1.xlsx")
        b2 = _new_batch(db, ImportFileKind.TIKTOK_PAYOUTS, "p2.xlsx")
        db.add(Payout(
            import_batch_id=b1.id, payout_id="P-1",
            paid_at=datetime(2026, 5, 1), net_amount=Decimal("100"),
        ))
        db.add(Payout(
            import_batch_id=b2.id, payout_id="P-2",
            paid_at=datetime(2026, 5, 2), net_amount=Decimal("200"),
        ))
        db.commit()

        delete_batch(db, db.get(ImportBatch, b1.id))
        db.commit()

        assert db.query(Payout).count() == 1
        assert db.query(Payout).one().payout_id == "P-2"


def test_delete_samples():
    with SessionLocal() as db:
        b = _new_batch(db, ImportFileKind.SAMPLES, "samples.csv")
        db.add(Sample(
            import_batch_id=b.id,
            shipped_at=datetime(2026, 5, 1),
            sku="SBX-A",
            quantity=2,
        ))
        db.commit()

        delete_batch(db, db.get(ImportBatch, b.id))
        db.commit()

        assert db.query(Sample).count() == 0


def test_delete_route_redirects_and_cascades(monkeypatch, tmp_path):
    """End-to-end: POST /uploads/{id}/delete triggers the service cascade."""
    from fastapi.testclient import TestClient

    from app.main import app

    with SessionLocal() as db:
        b = _new_batch(db, ImportFileKind.SAMPLES, "samples.csv")
        db.add(Sample(
            import_batch_id=b.id,
            shipped_at=datetime(2026, 5, 1),
            sku="SBX-A",
            quantity=2,
        ))
        db.commit()
        batch_id = b.id

    client = TestClient(app)
    r = client.post(f"/uploads/{batch_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/uploads"

    with SessionLocal() as db:
        assert db.query(Sample).count() == 0
        assert db.query(ImportBatch).count() == 0


def test_delete_route_404_on_missing_batch():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/uploads/9999/delete", follow_redirects=False)
    assert r.status_code == 404


def test_delete_catalog_is_audit_only():
    """Sku/Bundle rows have no import_batch_id, so they MUST survive a
    catalog-batch delete. Only the audit row is removed."""
    with SessionLocal() as db:
        # SKU master case
        b = _new_batch(db, ImportFileKind.SKU_MASTER, "skus.xlsx")
        db.add(Sku(sku="SBX-A", name="Lipstick", brand="smashbox", unit_cogs=Decimal("2")))
        db.commit()

        result = delete_batch(db, db.get(ImportBatch, b.id))
        db.commit()

        assert result.audit_only is True
        assert result.rows_deleted == 0
        assert db.query(Sku).count() == 1   # SURVIVES
        assert db.query(ImportBatch).count() == 0

        # Bundle mapping case
        b = _new_batch(db, ImportFileKind.BUNDLE_MAPPING, "bundles.xlsx")
        db.add(Bundle(tiktok_sku_id="X-1", name="Set", brand="smashbox"))
        db.commit()

        result = delete_batch(db, db.get(ImportBatch, b.id))
        db.commit()

        assert result.audit_only is True
        assert db.query(Bundle).count() == 1
