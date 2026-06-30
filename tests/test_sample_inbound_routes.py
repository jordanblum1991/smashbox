"""Sample inbound order routes: create / list / receive (clears) / delete, and
the inbound columns on the Sample Inventory page."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.sample_inbound_order import SampleInboundOrder
from app.models.shop import Shop
from app.models.sku import Sku
from app.reports.sample_inbound import compute_sample_inbound


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.add(Sku(sku="SBX-A", name="Primer A", brand="smashbox", tiktok_sku_id="111"))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_create_then_receive_clears_inbound_then_delete(client):
    r = client.post("/admin/sample-inbound", follow_redirects=False, data={
        "source": "Acme", "note": "",
        "sku": ["SBX-A", ""],                    # blank row skipped
        "quantity": ["3", ""],
    })
    assert r.status_code == 303
    with SessionLocal() as db:
        orders = db.execute(select(SampleInboundOrder)).scalars().all()
        assert len(orders) == 1
        oid = orders[0].id
        assert orders[0].unit_count == 3 and orders[0].source == "Acme"
        assert compute_sample_inbound(db)["SBX-A"] == 3   # counts as inbound

    assert client.get("/admin/sample-inbound").status_code == 200

    r = client.post(f"/admin/sample-inbound/{oid}/receive", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(SampleInboundOrder, oid).is_received
        assert compute_sample_inbound(db) == {}           # cleared on receipt

    r = client.post(f"/admin/sample-inbound/{oid}/delete", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(SampleInboundOrder, oid) is None


def test_create_with_no_lines_is_rejected(client):
    r = client.post("/admin/sample-inbound", follow_redirects=False, data={
        "source": "x", "sku": [""], "quantity": [""],
    })
    assert r.status_code == 303 and "error=" in r.headers["location"]
    with SessionLocal() as db:
        assert db.execute(select(SampleInboundOrder)).scalars().all() == []


def test_sample_inventory_page_shows_inbound_columns_and_link(client):
    r = client.get("/reports/sample-inventory")
    assert r.status_code == 200
    assert "Inbound" in r.text
    assert "/admin/sample-inbound" in r.text


def test_likely_received_surfaces_on_both_pages(client):
    from datetime import datetime
    from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
    from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
    from app.models.sample_inventory_snapshot import SampleInventorySnapshot
    with SessionLocal() as db:
        b1 = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT, status=ImportBatchStatus.COMPLETED,
                         original_filename="t", stored_path="t")
        db.add(b1); db.flush()
        db.add(SampleInventorySnapshot(import_batch_id=b1.id, sku="SBX-A", on_hand=5,
                                       captured_at=datetime(2026, 6, 20, 7, 0)))
        o = SampleInboundOrder(source="x", status="open", created_at=datetime(2026, 6, 21, 9, 0))
        db.add(o); db.flush()
        db.add(SampleInboundOrderLine(sample_inbound_order_id=o.id, sku="SBX-A", quantity=10))
        b2 = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT, status=ImportBatchStatus.COMPLETED,
                         original_filename="t", stored_path="t")
        db.add(b2); db.flush()
        db.add(SampleInventorySnapshot(import_batch_id=b2.id, sku="SBX-A", on_hand=15,
                                       captured_at=datetime(2026, 6, 25, 7, 0)))   # grew → looks received
        db.commit()

    assert "Looks received" in client.get("/admin/sample-inbound").text
    rep = client.get("/reports/sample-inventory").text
    assert "look" in rep and "/admin/sample-inbound" in rep
