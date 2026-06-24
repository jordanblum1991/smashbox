"""SKU-report CSV download on the Sales page.

The header "Download CSV" button must export the data for the ACTIVE tab. On
the SKUs tab it previously fell back to the Overview velocity CSV, so the
per-SKU table couldn't be downloaded at all. `/reports/sales.csv?tab=skus`
must return the SKU-performance table for the on-screen period.
"""
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


def _seed(db):
    db.add(Sku(sku="SBX-PRIMER", name="Photo Finish Primer", brand="smashbox",
               tiktok_sku_id="TT123", unit_cogs=Decimal("0")))
    db.flush()
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                    status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b)
    db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(2026, 5, 20, 12, 0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal("100"))
    db.add(o)
    db.flush()
    db.add(OrderLine(order_id=o.id, sku="TT123", quantity=4,
                     gross_sales=Decimal("100")))
    db.commit()


def test_sales_csv_tab_skus_returns_sku_performance_table():
    with SessionLocal() as db:
        _seed(db)

    client = TestClient(app)
    r = client.get("/reports/sales.csv",
                   params={"granularity": "daily",
                           "start_date": "2026-05-16", "end_date": "2026-05-31",
                           "tab": "skus"})
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    # SKU-table columns the velocity CSV does NOT have.
    assert "TikTok SKU ID" in body
    assert "Net Sales" in body
    # The seeded SKU and its units appear.
    assert "SBX-PRIMER" in body
    assert "Photo Finish Primer" in body
    assert "TT123" in body


def test_sales_csv_default_tab_is_still_velocity():
    """Regression guard: the Overview download is unchanged."""
    with SessionLocal() as db:
        _seed(db)

    client = TestClient(app)
    r = client.get("/reports/sales.csv", params={"granularity": "daily",
                                                  "start_date": "2026-05-16",
                                                  "end_date": "2026-05-31"})
    assert r.status_code == 200
    # Velocity CSV is period-bucketed and has no per-SKU TikTok ID column.
    assert "TikTok SKU ID" not in r.text
