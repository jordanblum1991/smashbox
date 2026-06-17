"""Dashboard surfaces a single "open items → Action Center" entry banner driven
by the per-request action_items count, so anything needing attention is one click
from the home page. The SKU Profitability report was fully decommissioned
(report unused) — no nav link and the route no longer exists."""
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
from app.models.shop import Shop
from app.models.sku import Sku
from app.reports.inventory_alerts import _reset_cache


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _reset_cache()
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        # A non-empty catalog: count_unmapped_skus stays quiet until a SKU master
        # exists, so seed one mapped SKU. The unmapped line below won't match it.
        db.add(Sku(sku="SBX-001", name="Primer", brand="smashbox",
                   tiktok_sku_id="SBX-001", unit_cogs=Decimal("5.00")))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_sku_profitability_fully_removed(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/reports/sku-profitability" not in r.text  # no nav link
    assert client.get("/reports/sku-profitability").status_code == 404  # route deleted


def test_no_action_banner_when_clean(client):
    r = client.get("/")
    assert r.status_code == 200
    # No open items → no entry banner (the nav link may still be present).
    assert "Open Action Center" not in r.text
    assert "need attention" not in r.text and "needs attention" not in r.text


def test_action_banner_shows_for_open_items(client):
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="o.csv", stored_path="/tmp/o.csv")
        db.add(b)
        db.flush()
        order = Order(import_batch_id=b.id, tiktok_order_id="O-1",
                      placed_at=datetime(2026, 5, 1), order_type=OrderType.PAID,
                      status="Completed", brand="smashbox")
        db.add(order)
        db.flush()
        db.add(OrderLine(order_id=order.id, sku="UNMAPPED-XYZ", quantity=2))
        db.commit()

    r = client.get("/")
    assert r.status_code == 200
    assert "attention" in r.text                  # "N item(s) need attention"
    assert "Open Action Center" in r.text
    assert 'href="/action-center"' in r.text
