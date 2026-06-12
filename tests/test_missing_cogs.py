"""Data-health: missing-COGS flags only in-play SKUs (on-hand or sold) at $0 cost."""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, InventorySnapshot, Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.missing_cogs import count_missing_cogs, find_missing_cogs


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, code, cogs):
    db.add(Sku(sku=code, name=code, brand="smashbox", tiktok_sku_id=code, unit_cogs=Decimal(cogs)))


def _snap(db, sku, on_hand):
    b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT, status=ImportBatchStatus.COMPLETED,
                    original_filename="x", stored_path="")
    db.add(b); db.flush()
    db.add(InventorySnapshot(import_batch_id=b.id, sku=sku, on_hand=on_hand,
                             captured_at=datetime(2026, 6, 1)))


def test_flags_zero_cogs_with_onhand_only():
    with SessionLocal() as db:
        _sku(db, "SBX-A", "0")     # 0 cost, has stock → flagged
        _sku(db, "SBX-B", "5.00")  # has cost → never flagged
        _sku(db, "SBX-C", "0")     # 0 cost, no stock, no sales → NOT flagged (dormant)
        db.flush()
        _snap(db, "SBX-A", 100)
        _snap(db, "SBX-B", 50)
        db.commit()
    with SessionLocal() as db:
        rows = find_missing_cogs(db)
        assert {r.sku_code for r in rows} == {"SBX-A"}
        assert rows[0].on_hand == 100
        assert count_missing_cogs(db) == 1


def test_flags_zero_cogs_with_sales():
    with SessionLocal() as db:
        _sku(db, "SBX-SOLD", "0")
        db.flush()
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="x", stored_path="")
        db.add(b); db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id="O1", placed_at=datetime(2026, 5, 1),
                  order_type=OrderType.PAID, status="Completed", brand="smashbox",
                  gross_sales=Decimal("10"))
        o.lines = [OrderLine(sku="SBX-SOLD", quantity=7, unit_price=Decimal("10"),
                             gross_sales=Decimal("10"))]
        db.add(o); db.commit()
    with SessionLocal() as db:
        rows = find_missing_cogs(db)
        assert len(rows) == 1 and rows[0].units_sold == 7
