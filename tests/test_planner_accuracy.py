"""Planner Accuracy page — wraps the backtest harness for the UI."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.models.import_batch import _utc_now_naive
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.reports.planner_accuracy import compute_planner_accuracy


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_no_data_when_no_snapshots():
    with SessionLocal() as db:
        v = compute_planner_accuracy(db)
    assert v.has_data is False


def test_route_renders_empty_state():
    r = TestClient(app).get("/reports/planner-accuracy")
    assert r.status_code == 200
    assert "Planner Accuracy" in r.text


def test_has_data_with_snapshot_and_following_demand():
    now = _utc_now_naive()
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", tiktok_sku_id="SBX-A", name="A",
                   brand="smashbox", unit_cogs=Decimal("5")))
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        db.add(InventorySnapshot(import_batch_id=b.id, sku="SBX-A", on_hand=100,
                                 captured_at=now - timedelta(days=40)))
        bo = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                         status=ImportBatchStatus.COMPLETED, original_filename="o", stored_path="o")
        db.add(bo); db.flush()
        for d in (38, 30, 20, 10, 5):
            o = Order(import_batch_id=bo.id, tiktok_order_id=f"O{d}",
                      placed_at=now - timedelta(days=d), order_type=OrderType.PAID,
                      status="Completed", brand="smashbox", gross_sales=Decimal("10"))
            db.add(o); db.flush()
            db.add(OrderLine(order_id=o.id, sku="SBX-A", quantity=2))
        db.commit()
        v = compute_planner_accuracy(db)
    assert v.has_data is True
    assert v.scorecard is not None
    assert v.as_of is not None
    assert v.verdict  # a plain-English read is always set
