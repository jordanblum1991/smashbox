# tests/test_sales_skus_tab.py
"""The SKUs tab on /reports/sales: route wires compute_sku_performance + the
template renders the table/insights; Overview stays the default."""
import itertools
from datetime import date, datetime
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
    db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox", tiktok_sku_id="S1",
               unit_cogs=Decimal("0")))
    db.flush()
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime.now().replace(hour=12, minute=0, second=0, microsecond=0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal("100"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=5, gross_sales=Decimal("100")))
    db.flush()


def test_default_tab_is_overview(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text          # Overview content present


def test_skus_tab_renders_table_and_insights(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "SBX-1" in r.text                       # the SKU code in the table
    assert "Primer" in r.text                       # the SKU name
    assert "Top seller" in r.text or "Top:" in r.text   # insights strip


def test_skus_tab_renders_stats_detail(client):
    """Each SKU row exposes the expandable granular-stats detail panel."""
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "toggleSkuDetail" in r.text          # expand wiring present
    assert "Avg units/day" in r.text            # a stat label in the panel


def test_skus_tab_sort_and_inactive_params_accepted(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    assert client.get("/reports/sales?tab=skus&sort=net_sales").status_code == 200
    assert client.get("/reports/sales?tab=skus&show_inactive=1").status_code == 200
