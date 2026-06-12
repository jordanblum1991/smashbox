"""Dashboard surfaces a consolidated data-health banner (unmapped SKUs, missing
COGS, policy violations, settlement orphans) so data-quality issues that distort
the P&L are visible on the home page, not just on the nav badge. Also guards the
SKU Profitability report — previously reachable only by URL — from the nav."""
from datetime import datetime

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
from decimal import Decimal


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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


def test_nav_links_sku_profitability_and_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/reports/sku-profitability" in r.text  # now discoverable from the nav
    assert client.get("/reports/sku-profitability").status_code == 200


def test_no_data_health_banner_when_clean(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Data health:" not in r.text  # no false alarm on an empty DB


def test_data_health_banner_shows_unmapped(client):
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
    assert "Data health:" in r.text
    assert "unmapped SKU" in r.text
    assert 'href="/reports/recon-health?tab=data-health"' in r.text
