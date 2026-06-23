# tests/test_sales_heatmap_tab.py
"""The Heatmap tab on /reports/sales renders the grid + dim toggle; the tab is a
real link; Overview/SKUs/Timing are unaffected."""
import itertools
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db):
    db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox", tiktok_sku_id="S1", unit_cogs=Decimal("0")))
    db.flush()
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime.now().replace(hour=12, minute=0, second=0, microsecond=0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox", gross_sales=Decimal("50"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=5, gross_sales=Decimal("50")))
    db.flush()


def test_heatmap_tab_renders(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=heatmap")
    assert r.status_code == 200
    assert "SBX-1" in r.text                 # the SKU row
    assert "Day of week" in r.text           # the dim toggle
    assert "tab=heatmap" in r.text           # the tab is a real link


def test_heatmap_daypart_switch(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=heatmap&dim=daypart")
    assert r.status_code == 200
    assert "Morning" in r.text and "Evening" in r.text   # daypart columns


def test_other_tabs_unaffected(client):
    assert "Revenue velocity" in client.get("/reports/sales").text
    assert "Showing" in client.get("/reports/sales?tab=skus").text
    assert "Day of week" in client.get("/reports/sales?tab=timing").text
