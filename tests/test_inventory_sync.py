"""SAP inventory sync — warehouse filtering, idempotency, failure handling, and
the schedule-settings route."""
import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatchStatus, InventorySnapshot
from app.models.shop import Shop
from app.services import inventory_sync


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.commit()
    yield


# Mirrors the real feed: 4 warehouses per SKU, OnHand as strings.
def _feed(date="2026-06-12 09:40:54.203"):
    rows = []
    for code, sb, sbs in [("SBX-A", "364", "240"), ("SBX-B", "0", "12"), ("SBX-C", "98", "0")]:
        rows.append({"Itemcode": code, "WhsCode": "01", "OnHand": "0", "InventoryDate": date})
        rows.append({"Itemcode": code, "WhsCode": "MIA", "OnHand": "5", "InventoryDate": date})
        rows.append({"Itemcode": code, "WhsCode": "SB", "OnHand": sb, "InventoryDate": date})
        rows.append({"Itemcode": code, "WhsCode": "SBS", "OnHand": sbs, "InventoryDate": date})
    return rows


def test_keeps_only_sb_warehouse(monkeypatch):
    monkeypatch.setattr(inventory_sync, "fetch_sap_inventory", lambda url: _feed())
    with SessionLocal() as db:
        batch = inventory_sync.sync_inventory_from_sap(db, source="test")
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.rows_imported == 3  # one SB row per SKU; MIA/01/SBS dropped

    with SessionLocal() as db:
        rows = {r.sku: r.on_hand for r in db.query(InventorySnapshot).all()}
        assert rows == {"SBX-A": 364, "SBX-B": 0, "SBX-C": 98}  # SB values, not SBS
        # captured_at truncates to the feed date.
        from datetime import datetime
        assert all(r.captured_at == datetime(2026, 6, 12)
                   for r in db.query(InventorySnapshot).all())


def test_resync_same_day_is_idempotent(monkeypatch):
    monkeypatch.setattr(inventory_sync, "fetch_sap_inventory", lambda url: _feed())
    with SessionLocal() as db:
        inventory_sync.sync_inventory_from_sap(db, source="test")
    # Second pull, same date but new SB qty for SBX-A.
    bumped = [dict(r, OnHand="500") if r["Itemcode"] == "SBX-A" and r["WhsCode"] == "SB" else r
              for r in _feed()]
    monkeypatch.setattr(inventory_sync, "fetch_sap_inventory", lambda url: bumped)
    with SessionLocal() as db:
        inventory_sync.sync_inventory_from_sap(db, source="test")

    with SessionLocal() as db:
        rows = db.query(InventorySnapshot).all()
        assert len(rows) == 3  # updated in place, no duplicate rows
        assert {r.sku: r.on_hand for r in rows}["SBX-A"] == 500


def test_http_failure_records_failed_batch(monkeypatch):
    def boom(url):
        raise RuntimeError("503 from SAP")
    monkeypatch.setattr(inventory_sync, "fetch_sap_inventory", boom)
    with SessionLocal() as db:
        batch = inventory_sync.sync_inventory_from_sap(db, source="test")
        assert batch.status == ImportBatchStatus.FAILED
        assert "503 from SAP" in batch.error_message
    with SessionLocal() as db:
        assert db.query(InventorySnapshot).count() == 0  # nothing partially written


def test_non_list_response_fails_cleanly(monkeypatch):
    monkeypatch.setattr(inventory_sync, "fetch_sap_inventory",
                        lambda url: (_ for _ in ()).throw(ValueError("expected a JSON list")))
    with SessionLocal() as db:
        batch = inventory_sync.sync_inventory_from_sap(db, source="test")
        assert batch.status == ImportBatchStatus.FAILED


def test_settings_route_updates_schedule(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/uploads/inventory-sync-settings",
        data={"sync_time": "06:15", "enabled": "1", "days": ["mon", "wed", "fri"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).one()
        assert shop.inventory_sync_enabled is True
        assert (shop.inventory_sync_hour, shop.inventory_sync_minute) == (6, 15)
        assert shop.inventory_sync_days == "mon,wed,fri"


def test_settings_route_no_days_disables(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/uploads/inventory-sync-settings",
        data={"sync_time": "06:15", "enabled": "1"},  # no days ticked
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).one()
        assert shop.inventory_sync_enabled is False  # empty day set => disabled
