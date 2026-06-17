"""Ad Spend & Campaign KPIs page, sourced from the imported daily GMV Max
report. By-month / All-Time / Date-range scopes; highlighted totals; Excel
download. No ad-credit info or Reimbursements link.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.order import Order, OrderType


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _seed_day(db, d: date, cost, sku, gr, order_gross=None):
    b = _batch(db)
    db.add(GmvMaxDailyMetric(import_batch_id=b.id, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=sku, gross_revenue=Decimal(str(gr))))
    if order_gross is not None:
        db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{d.isoformat()}",
                     placed_at=datetime(d.year, d.month, d.day, 12, 0), order_type=OrderType.PAID,
                     status="Shipped", brand="smashbox", gross_sales=Decimal(str(order_gross))))
    db.flush()


def test_by_month_view_renders_table(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost=200, sku=10, gr=600, order_gross=1000)
        db.commit()
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "Monthly GMV Max KPIs" in r.text
    assert "$200.00" in r.text                # Total Gross Spend = campaign Cost
    assert "5.00x" in r.text                  # ROAS = net 1000 / cost 200
    assert "All-Time" in r.text               # toggle + totals row
    # Blended-ROAS relabel + scope footnote (honest labeling, not "ROAS").
    assert "Blended ROAS" in r.text
    assert "not</em> campaign-attributed" in r.text or "not campaign-attributed" in r.text
    assert "/reports/ad-spend/reimbursements" in r.text   # cross-link to full spend breakdown


def test_by_month_shows_campaign_columns(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost="7824.02", sku=413, gr="15769.65")
        db.commit()
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "413" in r.text          # SKU orders
    assert "$18.94" in r.text       # cost per order = 7824.02 / 413
    assert "2.02" in r.text         # ROI = 15769.65 / 7824.02


def test_all_time_view_collapses_to_totals(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost="7824.02", sku=413, gr="15769.65")
        db.commit()
    r = client.get("/reports/ad-spend?scope=all-time")
    assert r.status_code == 200
    assert "All-Time" in r.text
    assert "Monthly GMV Max KPIs" not in r.text    # per-month table hidden
    assert "413" in r.text
    assert "2.02" in r.text


def test_date_range_scopes_table(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 4, 15), cost=40, sku=4, gr=160)
        _seed_day(db, date(2026, 5, 15), cost=50, sku=5, gr=200)
        db.commit()
    r = client.get("/reports/ad-spend?scope=range&start_date=2026-05-01&end_date=2026-05-31")
    assert r.status_code == 200
    assert "May-2026" in r.text          # in window
    assert "Apr-2026" not in r.text      # excluded by the range
    assert "Range total" in r.text


def test_date_range_invalid_shows_error(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost=50, sku=5, gr=200)
        db.commit()
    r = client.get("/reports/ad-spend?scope=range&start_date=2026-05-10&end_date=2026-05-01")
    assert r.status_code == 200
    assert "Start date must be on or before end date" in r.text


def test_export_xlsx_downloads(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost="7824.02", sku=413, gr="15769.65")
        db.commit()
    r = client.get("/export/ad-spend.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert len(r.content) > 100

    r2 = client.get("/export/ad-spend.xlsx?scope=range&start_date=2026-05-01&end_date=2026-05-31")
    assert r2.status_code == 200
    assert "2026-05-01_to_2026-05-31" in r2.headers["content-disposition"]


def test_no_data_shows_empty_state(client):
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "No GMV Max campaign data yet" in r.text


def test_daily_scope_renders_per_day_rows(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 10), cost=100, sku=5, gr=300)
        _seed_day(db, date(2026, 5, 11), cost=50, sku=2, gr=120)
        db.commit()
    r = client.get("/reports/ad-spend?scope=daily&start_date=2026-05-10&end_date=2026-05-11")
    assert r.status_code == 200
    assert "Daily GMV Max KPIs" in r.text
    assert "May 10, 2026" in r.text          # range header
    assert "$150.00" in r.text               # range-total spend (100 + 50)
    # Daily view is attributed-only — no Blended ROAS column.
    assert "Blended ROAS" not in r.text


def test_daily_scope_defaults_window_when_no_dates(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 10), cost=100, sku=5, gr=300)
        db.commit()
    # No start/end → defaults to last 30 days of available data; should still render.
    r = client.get("/reports/ad-spend?scope=daily")
    assert r.status_code == 200
    assert "Daily GMV Max KPIs" in r.text


def test_daily_csv_exports_per_day(client):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 10), cost=100, sku=5, gr=300)
        db.commit()
    r = client.get("/reports/ad-spend-daily.csv?start_date=2026-05-10&end_date=2026-05-10")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Date,SKU Orders,Cost per Order,Gross Revenue,ROI,Gross Spend" in r.text
    assert "2026-05-10" in r.text


@pytest.mark.parametrize("url", ["/reports/ad-spend", "/reports/ad-spend?scope=all-time"])
def test_no_credit_info_or_reimbursements_link(client, url):
    with SessionLocal() as db:
        _seed_day(db, date(2026, 5, 15), cost=200, sku=10, gr=600)
        db.commit()
    r = client.get(url)
    assert r.status_code == 200
    assert "Total Ad Credits Applied" not in r.text
    assert "Net of Credits" not in r.text
    assert "Reimbursements →" not in r.text
