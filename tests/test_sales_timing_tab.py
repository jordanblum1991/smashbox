# tests/test_sales_timing_tab.py
"""The Timing tab on /reports/sales renders the callouts + charts; the tab is a
real link; Overview/SKUs are unaffected."""
import itertools
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _order(db, dt, rev):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
                 order_type=OrderType.PAID, status="Completed", brand="smashbox",
                 gross_sales=Decimal(str(rev)), shipping_revenue=Decimal("0"),
                 seller_funded_outlandish=Decimal("0"), seller_funded_smashbox=Decimal("0"),
                 platform_discount_total=Decimal("0"), payment_platform_discount=Decimal("0")))
    db.flush()


def test_timing_tab_renders(client):
    with SessionLocal() as db:
        _order(db, datetime.now().replace(hour=12, minute=0, second=0, microsecond=0), 100)
        db.commit()
    r = client.get("/reports/sales?tab=timing")
    assert r.status_code == 200
    assert "Day of week" in r.text          # the DOW panel heading
    assert "Time of day" in r.text          # the hour panel heading
    assert "tab=timing" in r.text           # the tab is a real link carrying itself


def test_overview_default_unaffected(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text     # Overview content still renders


def test_skus_tab_still_works(client):
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "Showing" in r.text              # the SKU pagination control still renders
