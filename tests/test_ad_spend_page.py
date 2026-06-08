"""Ad Spend Summary page.

Default (no period chosen) → a per-month overview (Month | Total Gross Spend |
ROAS). Picking a specific period → two aggregate KPIs for that period. Neither
view shows ad-credit info or an in-page Reimbursements link.
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


def test_default_view_shows_monthly_table(client):
    with SessionLocal() as db:
        _seed_month(db, "M", datetime(2026, 5, 15, 12, 0), gross=1000, spend=200)
        db.commit()
    r = client.get("/reports/ad-spend")          # bare — no period specified
    assert r.status_code == 200
    assert "Monthly ad spend" in r.text          # the per-month overview
    assert "ROAS" in r.text
    assert "$200.00" in r.text                    # the month's gross spend
    assert "5.00x" in r.text                      # 1000 / 200


def test_specific_period_shows_two_kpis(client):
    r = client.get("/reports/ad-spend?period=month&year=2026&month=5")
    assert r.status_code == 200
    assert "Total Gross Spend" in r.text
    assert "ROAS" in r.text
    assert "Monthly ad spend" not in r.text       # collapsed to the period KPIs


@pytest.mark.parametrize("url", ["/reports/ad-spend", "/reports/ad-spend?period=month&year=2026&month=5"])
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
