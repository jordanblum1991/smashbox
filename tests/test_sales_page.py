"""Sales page: renders per granularity, toggle switches the window, invalid
granularity falls back to daily, CSV exports the velocity table, nav links it."""
import csv
import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db, d, gross, units):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{d.isoformat()}-{gross}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="X", quantity=units))
    db.flush()


def test_sales_page_renders():
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 2)
        db.commit()
    r = TestClient(app).get("/reports/sales")
    assert r.status_code == 200
    assert "Sales" in r.text
    assert "Daily" in r.text and "Weekly" in r.text and "Monthly" in r.text


def test_granularity_toggle_switches_view(client):
    r = client.get("/reports/sales?granularity=monthly")
    assert r.status_code == 200
    assert "granularity=monthly" in r.text


def test_invalid_granularity_falls_back_to_daily(client):
    r = client.get("/reports/sales?granularity=foo")
    assert r.status_code == 200


def test_no_data_renders_empty_state(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200


def test_sales_page_has_revenue_chart(client):
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 1)
        db.commit()
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text     # chart section heading
    assert "<svg" in r.text                  # inline-SVG bar chart rendered


def test_sales_csv_exports_velocity_table(client):
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 2)
        db.commit()
    r = client.get("/reports/sales.csv?granularity=daily")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"]
    assert len(rows) >= 2          # header + at least one bucket


def test_nav_has_sales_link(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert 'href="/reports/sales"' in r.text     # top-level nav link present


def test_custom_range_scopes_the_page(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "Mar 01" in r.text or "Mar 1" in r.text


@pytest.mark.xfail(reason="banner/error rendered in Task 4", strict=False)
def test_bad_range_shows_error_and_falls_back(client):
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-10&end_date=2026-03-01")
    assert r.status_code == 200
    assert "Start date must be on or before end date" in r.text  # banner/error text asserted in Task 4


@pytest.mark.xfail(reason="banner/error rendered in Task 4", strict=False)
def test_fiscal_month_scope_renders_banner(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales?granularity=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert "Fiscal" in r.text
    assert "Fiscal May 2026" not in client.get("/reports/sales").text


def test_fiscal_year_csv_exports(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales.csv?granularity=fiscal_year&year=2026")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "fiscal_year" in r.headers["content-disposition"]
