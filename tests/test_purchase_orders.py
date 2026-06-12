"""Purchase Orders — draft create, editable lines, place (freeze), numbering."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.shop import Shop
from app.models.sku import Sku


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.add(Sku(sku="SBX-001", name="Primer", brand="smashbox",
                   tiktok_sku_id="SBX-001", unit_cogs=Decimal("5.00")))
        db.commit()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _sku_id():
    with SessionLocal() as db:
        return db.query(Sku).one().id


def test_blank_po_gets_sequential_number(client):
    for expected in ("PO-0001", "PO-0002"):
        r = client.post("/admin/purchase-orders/blank", follow_redirects=False)
        assert r.status_code == 303
    with SessionLocal() as db:
        nums = sorted(p.number for p in db.query(PurchaseOrder).all())
        assert nums == ["PO-0001", "PO-0002"]
        assert all(p.status == "draft" for p in db.query(PurchaseOrder).all())


def test_add_edit_delete_line(client):
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        po_id = db.query(PurchaseOrder).one().id

    # Add a line — unit cost defaults to the SKU's COGS.
    client.post(f"/admin/purchase-orders/{po_id}/lines",
                data={"sku_id": str(_sku_id()), "quantity": "10"})
    with SessionLocal() as db:
        ln = db.query(PurchaseOrderLine).one()
        assert (ln.sku, ln.quantity, ln.unit_cost) == ("SBX-001", 10, Decimal("5.0000"))
        line_id, po = ln.id, db.get(PurchaseOrder, po_id)
        assert po.total == Decimal("50.00") and po.unit_count == 10

    # Edit qty + cost.
    client.post(f"/admin/purchase-orders/{po_id}/lines/{line_id}/edit",
                data={"quantity": "12", "unit_cost": "4.50"})
    with SessionLocal() as db:
        assert db.get(PurchaseOrder, po_id).total == Decimal("54.00")  # 12 × 4.50

    # Delete.
    client.post(f"/admin/purchase-orders/{po_id}/lines/{line_id}/delete")
    with SessionLocal() as db:
        assert db.query(PurchaseOrderLine).count() == 0


def test_place_freezes_po(client):
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        po_id = db.query(PurchaseOrder).one().id
    client.post(f"/admin/purchase-orders/{po_id}/lines",
                data={"sku_id": str(_sku_id()), "quantity": "3"})
    with SessionLocal() as db:
        line_id = db.query(PurchaseOrderLine).one().id

    r = client.post(f"/admin/purchase-orders/{po_id}/place", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        po = db.get(PurchaseOrder, po_id)
        assert po.status == "placed" and po.placed_at is not None

    # Editing a placed PO is rejected.
    r = client.post(f"/admin/purchase-orders/{po_id}/lines/{line_id}/edit",
                    data={"quantity": "9", "unit_cost": "1"})
    assert r.status_code == 400


def test_cannot_place_empty_po(client):
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        po_id = db.query(PurchaseOrder).one().id
    client.post(f"/admin/purchase-orders/{po_id}/place", follow_redirects=False)
    with SessionLocal() as db:
        assert db.get(PurchaseOrder, po_id).status == "draft"  # blocked, still draft


def test_from_plan_creates_draft(client):
    # No orders/velocity in this DB → empty recommendations → empty draft PO.
    r = client.post("/admin/purchase-orders/from-plan", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.query(PurchaseOrder).count() == 1
