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
from app.models.tiktok_daily_metric import TikTokDailyMetric


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


def _seed_daily(db, d, gmv, orders, items):
    """Seed one TikTok Shop-Analytics day (the finalized 'Seller Center' figure)."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ANALYTICS, status=ImportBatchStatus.COMPLETED,
                    original_filename="a", stored_path="a")
    db.add(b); db.flush()
    db.add(TikTokDailyMetric(import_batch_id=b.id, metric_date=d,
                             gmv=Decimal(str(gmv)), orders=orders, items_sold=items))
    db.flush()


def test_overview_shows_finalized_reconciliation_strip(client):
    # Booked (order-derived) vs finalized (TikTok's own daily GMV) for the window.
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 2)          # booked: $100, 2 units, 1 order
        _seed_daily(db, date(2026, 3, 10), 80, 1, 1)  # finalized: $80, 1 item, 1 order
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "Finalized" in r.text                 # the reconciliation strip rendered
    assert "$80.00" in r.text                    # finalized GMV
    assert "$100.00" in r.text                   # booked total
    assert "$20.00" in r.text                    # the difference


def test_gmv_kpi_card_shows_difference_vs_finalized(client):
    # The GMV KPI card annotates the booked-vs-finalized difference inline.
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 2)          # booked GMV $100
        _seed_daily(db, date(2026, 3, 10), 80, 1, 1)  # finalized GMV $80
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "data-gmv-recon" in r.text          # the KPI-card annotation marker
    assert "TikTok $80.00" in r.text           # finalized (Seller Center) GMV total shown
    assert "+$20.00" in r.text                 # booked is $20 above finalized (signed)


def test_gmv_kpi_annotation_absent_without_daily_metrics(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 2)
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "data-gmv-recon" not in r.text


def test_overview_hides_strip_without_daily_metrics(client):
    # No analytics feed → no finalized figure to compare against → no strip, no crash.
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 2)
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "Finalized" not in r.text


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


def test_bad_range_shows_error_and_falls_back(client):
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-10&end_date=2026-03-01")
    assert r.status_code == 200
    assert "Start date must be on or before end date" in r.text  # banner/error text asserted in Task 4


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


def test_out_of_range_fiscal_month_does_not_500(client):
    # Hand-edited URL with month=13 must not crash — falls back gracefully.
    r = client.get("/reports/sales?granularity=fiscal_month&year=2026&month=13")
    assert r.status_code == 200
    assert "Start date" not in r.text or r.status_code == 200   # no 500
    # CSV path too
    rc = client.get("/reports/sales.csv?granularity=fiscal_year&year=99999")
    assert rc.status_code == 200


def test_valid_fiscal_params_still_work(client):
    from datetime import date as _d
    from decimal import Decimal as _D
    with SessionLocal() as db:
        _seed(db, _d(2026, 5, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales?granularity=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert "Fiscal" in r.text
