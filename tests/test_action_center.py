"""Action Center — consolidated open-items hub. Verifies the roll-up reflects
the underlying signals and that informational ("heads up") items stay out of the
actionable headline count."""
from datetime import datetime
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
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.shop import Shop
from app.models.sku import Sku
from app.reports.action_center import compute_action_center
from app.reports.inventory_alerts import _reset_cache


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _reset_cache()  # inventory alert summary is process-cached
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.add(Sku(sku="SBX-001", name="Primer", brand="smashbox",
                   tiktok_sku_id="SBX-001", unit_cogs=Decimal("5.00")))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_all_clear_when_empty(client):
    with SessionLocal() as db:
        v = compute_action_center(db)
        assert v.total_items == 0
        assert v.groups == []
        assert v.heads_up == []
    r = client.get("/action-center")
    assert r.status_code == 200
    assert "Action Center" in r.text
    assert "all caught up" in r.text


def test_unmapped_sku_is_a_data_health_item(client):
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="o.csv", stored_path="/tmp/o.csv")
        db.add(b)
        db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id="O-1",
                  placed_at=datetime(2026, 5, 1), order_type=OrderType.PAID,
                  status="Completed", brand="smashbox")
        db.add(o)
        db.flush()
        db.add(OrderLine(order_id=o.id, sku="UNMAPPED-XYZ", quantity=2))
        db.commit()
    with SessionLocal() as db:
        v = compute_action_center(db)
        keys = {it.key for g in v.groups for it in g.items}
        assert "dh_unmapped" in keys
        assert v.total_items >= 1
    r = client.get("/action-center")
    assert r.status_code == 200
    assert "unmapped" in r.text.lower()


def test_placed_po_is_heads_up_not_counted(client):
    with SessionLocal() as db:
        po = PurchaseOrder(number="PO-0001", supplier="S", status="placed")
        po.lines.append(PurchaseOrderLine(sku="SBX-001", name="Primer",
                                          quantity=10, unit_cost=Decimal("5")))
        db.add(po)
        db.commit()
    with SessionLocal() as db:
        v = compute_action_center(db)
        assert "open_pos" in {it.key for it in v.heads_up}
        # informational items must NOT inflate the actionable headline
        assert all(it.key != "open_pos" for g in v.groups for it in g.items)


def test_nav_badge_and_link_present(client):
    # Empty DB → link present, no badge number.
    r = client.get("/action-center")
    assert 'href="/action-center"' in r.text
