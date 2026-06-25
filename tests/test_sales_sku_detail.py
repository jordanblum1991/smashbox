"""Per-SKU sales drill-down (sub-project C): row + 12-week trend + recent
orders + bundle membership, composed from the sales-lens data."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.bundle import Bundle, BundleComponent
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.sales_sku_detail import compute_sales_sku_detail

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                    status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    return b


def _order(db, b, d, sku, qty, gross, refund=0):
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross)), refunds=Decimal(str(refund)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku, quantity=qty,
                     gross_sales=Decimal(str(gross))))
    db.flush()


AS_OF = date(2026, 5, 31)


def test_detail_row_trend_and_recent_orders():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox",
                   tiktok_sku_id="S1", unit_cogs=Decimal("0")))
        db.flush()
        b = _batch(db)
        _order(db, b, date(2026, 5, 20), "S1", 3, 100)            # week of May 18
        _order(db, b, date(2026, 5, 27), "S1", 2, 50, refund=10)  # week of May 25
        db.commit()
        d = compute_sales_sku_detail(db, "S1", start=date(2026, 5, 1),
                                     end=date(2026, 5, 31), as_of=AS_OF)

    assert d.row is not None
    assert d.row.code == "SBX-1" and d.row.units == 5

    weeks = {w.week_start: w for w in d.weekly_trend}
    assert len(d.weekly_trend) == 12
    assert weeks[date(2026, 5, 18)].units == 3
    assert weeks[date(2026, 5, 18)].revenue == Decimal("100.00")
    assert weeks[date(2026, 5, 25)].units == 2
    assert weeks[date(2026, 5, 25)].revenue == Decimal("50.00")

    # Recent orders, newest first; the May 27 order is flagged refunded.
    assert [o.qty for o in d.recent_orders] == [2, 3]
    assert d.recent_orders[0].refunded is True
    assert d.recent_orders[1].refunded is False


def test_detail_bundle_membership():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox",
                   tiktok_sku_id="S1", unit_cogs=Decimal("0")))
        bundle = Bundle(bundle_sku="KIT-1", name="Starter Kit", brand="smashbox",
                        tiktok_sku_id="B1")
        db.add(bundle); db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="S1",
                               component_name="Primer", quantity=2))
        b = _batch(db)
        _order(db, b, date(2026, 5, 20), "S1", 1, 50)
        db.commit()
        d = compute_sales_sku_detail(db, "S1", start=date(2026, 5, 1),
                                     end=date(2026, 5, 31), as_of=AS_OF)

    assert any(p.bundle_sku == "KIT-1" for p in d.bundle_parents)


def test_drilldown_route_renders_page_and_planner_link():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-1", name="Photo Finish Primer", brand="smashbox",
                   tiktok_sku_id="S1", unit_cogs=Decimal("0")))
        db.flush()
        b = _batch(db)
        _order(db, b, date(2026, 5, 20), "S1", 3, 100)
        db.commit()
    client = TestClient(app)
    r = client.get("/reports/sales/sku/S1",
                   params={"granularity": "daily",
                           "start_date": "2026-05-01", "end_date": "2026-05-31"})
    assert r.status_code == 200
    assert "Photo Finish Primer" in r.text
    assert "Recent orders" in r.text
    # Cross-link to the demand-planner drill-down (the buying lens).
    assert "/reports/demand-planning/sku/S1" in r.text


def test_detail_none_row_for_inactive_sku():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox",
                   tiktok_sku_id="S1", unit_cogs=Decimal("0")))
        db.commit()
        d = compute_sales_sku_detail(db, "S1", start=date(2026, 5, 1),
                                     end=date(2026, 5, 31), as_of=AS_OF)
    assert d.row is None
    assert d.recent_orders == []
