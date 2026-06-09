"""Ad Spend & Campaign KPIs page.

A By-month / All-Time toggle. "By month" shows one row per month (SKU Orders,
Cost per Order, Gross Revenue, ROI, Total Gross Spend, ROAS) with a highlighted
all-time totals row. "All-Time" collapses to the combined figures. Spend is
GMV-Max only; no ad-credit info or Reimbursements link on this page.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_spend import AdSpend
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.models.order import Order, OrderType


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed_month(db, oid, placed, gross, spend):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=oid, placed_at=placed,
                 order_type=OrderType.PAID, status="Shipped", brand="smashbox",
                 gross_sales=Decimal(str(gross))))
    db.add(AdSpend(import_batch_id=b.id, spend_date=placed, campaign_id="C1",
                   amount=Decimal(str(spend))))
    db.flush()


def _seed_campaign(db, year, month, gross_revenue, sku_orders):
    db.add(GmvMaxCampaignMetric(year=year, month=month,
                                gross_revenue=Decimal(str(gross_revenue)), sku_orders=sku_orders))
    db.flush()


def test_by_month_view_renders_table(client):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=1000, spend=200)
        db.commit()
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "Monthly GMV Max KPIs" in r.text       # the per-month table
    assert "$200.00" in r.text                     # GMV-Max spend column
    assert "5.00x" in r.text                       # ROAS 1000 / 200
    assert "All-Time" in r.text                    # toggle + highlighted totals row


def test_by_month_shows_campaign_columns(client):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=15769.65, spend="7824.02")
        _seed_campaign(db, 2026, 5, "15769.65", 413)
        db.commit()
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "413" in r.text          # SKU orders
    assert "$18.94" in r.text       # cost per order = 7824.02 / 413
    assert "2.02" in r.text         # ROI = 15769.65 / 7824.02


def test_all_time_view_collapses_to_totals(client):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=15769.65, spend="7824.02")
        _seed_campaign(db, 2026, 5, "15769.65", 413)
        db.commit()
    r = client.get("/reports/ad-spend?scope=all-time")
    assert r.status_code == 200
    assert "All-Time" in r.text
    assert "Monthly GMV Max KPIs" not in r.text    # per-month table hidden
    assert "413" in r.text
    assert "2.02" in r.text


def test_date_range_scopes_table(client):
    with SessionLocal() as db:
        _seed_month(db, "APR", datetime(2026, 4, 15, 12, 0), gross=400, spend=40)
        _seed_month(db, "MAY", datetime(2026, 5, 15, 12, 0), gross=500, spend=50)
        db.commit()
    r = client.get("/reports/ad-spend?scope=range&start_date=2026-05-01&end_date=2026-05-31")
    assert r.status_code == 200
    assert "May-2026" in r.text          # in window
    assert "Apr-2026" not in r.text      # excluded by the range
    assert "Range total" in r.text


def test_date_range_invalid_shows_error(client):
    with SessionLocal() as db:
        _seed_month(db, "MAY", datetime(2026, 5, 15, 12, 0), gross=500, spend=50)
        db.commit()
    r = client.get("/reports/ad-spend?scope=range&start_date=2026-05-10&end_date=2026-05-01")
    assert r.status_code == 200
    assert "Start date must be on or before end date" in r.text


def test_no_data_shows_empty_state(client):
    r = client.get("/reports/ad-spend")
    assert r.status_code == 200
    assert "No ad spend imported yet" in r.text


def test_export_xlsx_downloads(client):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=15769.65, spend="7824.02")
        _seed_campaign(db, 2026, 5, "15769.65", 413)
        db.commit()
    r = client.get("/export/ad-spend.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert len(r.content) > 100          # a real xlsx payload

    # Range scope flows through to the filename.
    r2 = client.get("/export/ad-spend.xlsx?scope=range&start_date=2026-05-01&end_date=2026-05-31")
    assert r2.status_code == 200
    assert "2026-05-01_to_2026-05-31" in r2.headers["content-disposition"]


@pytest.mark.parametrize("url", ["/reports/ad-spend", "/reports/ad-spend?scope=all-time"])
def test_no_credit_info_or_reimbursements_link(client, url):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=1000, spend=200)
        db.commit()
    r = client.get(url)
    assert r.status_code == 200
    assert "Total Ad Credits Applied" not in r.text
    assert "Net of Credits" not in r.text
    assert "Ad Credits Applied" not in r.text
    assert "Reimbursements →" not in r.text
