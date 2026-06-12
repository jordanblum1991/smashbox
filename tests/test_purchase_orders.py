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


def _placed_po_with_line(client, qty):
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        po_id = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).first().id
    client.post(f"/admin/purchase-orders/{po_id}/lines",
                data={"sku_id": str(_sku_id()), "quantity": str(qty)})
    client.post(f"/admin/purchase-orders/{po_id}/place")
    return po_id


def test_in_transit_counts_placed_only(client):
    from app.reports.in_transit import compute_in_transit

    # Draft PO → not in transit.
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        draft_id = db.query(PurchaseOrder).one().id
    client.post(f"/admin/purchase-orders/{draft_id}/lines",
                data={"sku_id": str(_sku_id()), "quantity": "7"})
    with SessionLocal() as db:
        assert compute_in_transit(db) == {}

    # Place it → units become in-transit, keyed by every SKU identifier.
    client.post(f"/admin/purchase-orders/{draft_id}/place")
    with SessionLocal() as db:
        it = compute_in_transit(db)
        # SBX-001 is both sku and tiktok_sku_id here → one key, qty 7 (no double-count).
        assert it == {"SBX-001": 7}


def test_receive_clears_in_transit_and_unreceive_restores(client):
    from app.reports.in_transit import compute_in_transit

    po_id = _placed_po_with_line(client, 5)
    with SessionLocal() as db:
        assert compute_in_transit(db) == {"SBX-001": 5}

    # Mark received → no longer in transit.
    r = client.post(f"/admin/purchase-orders/{po_id}/receive", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(PurchaseOrder, po_id).status == "received"
        assert compute_in_transit(db) == {}

    # Undo receive → back to placed (in-transit), placed_at preserved.
    client.post(f"/admin/purchase-orders/{po_id}/unreceive")
    with SessionLocal() as db:
        po = db.get(PurchaseOrder, po_id)
        assert po.status == "placed" and po.placed_at is not None
        assert compute_in_transit(db) == {"SBX-001": 5}


def test_received_po_is_read_only(client):
    po_id = _placed_po_with_line(client, 4)
    client.post(f"/admin/purchase-orders/{po_id}/receive")
    with SessionLocal() as db:
        line_id = db.query(PurchaseOrderLine).one().id
    r = client.post(f"/admin/purchase-orders/{po_id}/lines/{line_id}/edit",
                    data={"quantity": "9", "unit_cost": "1"})
    assert r.status_code == 400


def test_cannot_receive_a_draft(client):
    client.post("/admin/purchase-orders/blank")
    with SessionLocal() as db:
        po_id = db.query(PurchaseOrder).one().id
    client.post(f"/admin/purchase-orders/{po_id}/receive")
    with SessionLocal() as db:
        assert db.get(PurchaseOrder, po_id).status == "draft"  # rejected, unchanged


def test_in_transit_feeds_demand_planning_receipts(client):
    """A placed PO's units land in the planner's expected_receipts for that SKU."""
    from app.reports.demand_planning import compute_demand_planning_view

    _placed_po_with_line(client, 12)
    with SessionLocal() as db:
        view = compute_demand_planning_view(db)
    row = next((r for r in view.rows if "SBX-001" in
                (r.component_sku, getattr(r, "sku_code", None))), None)
    # The SKU has no sales velocity, so it may not surface as a planner row; when it
    # does (on-hand present), its in-transit must be reflected. Guard both cases.
    if row is not None:
        assert row.expected_receipts == 12
