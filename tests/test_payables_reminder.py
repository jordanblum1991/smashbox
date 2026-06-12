"""Payable reminders: the 'due soon' AP signal (compute + dashboard banner)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice
from app.reports.overdue_ap import compute_due_soon_ap
from app.services.reporting_tz import today_local


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _inv(db, number, due_offset_days, amount, status="open"):
    today = today_local()
    db.add(PurchaseInvoice(
        number=number,
        invoice_date=today - timedelta(days=40),
        due_date=today + timedelta(days=due_offset_days),
        amount=Decimal(amount),
        status=status,
    ))


def test_compute_due_soon_ap():
    with SessionLocal() as db:
        _inv(db, "SOON-5", 5, "500.00")        # due in 5 days  -> due soon
        _inv(db, "SOON-0", 0, "100.00")        # due today      -> due soon
        _inv(db, "FAR-30", 30, "300.00")       # due in 30 days -> not within 14
        _inv(db, "OVERDUE", -3, "999.00")      # past due       -> excluded
        db.commit()
        r = compute_due_soon_ap(db)            # default 14-day window
    assert r["within_days"] == 14
    assert r["count"] == 2
    assert r["total"] == Decimal("600.00")     # 500 + 100


def test_dashboard_shows_due_soon_banner(client: TestClient):
    with SessionLocal() as db:
        _inv(db, "SOON-7", 7, "456.00")
        db.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "Payables due soon" in r.text
    assert "$456.00" in r.text
    assert "within 14 days" in r.text


def test_no_due_soon_banner_for_overdue_only(client: TestClient):
    # An overdue invoice triggers the overdue banner but NOT the due-soon one.
    with SessionLocal() as db:
        _inv(db, "OD", -5, "111.00")
        db.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "Overdue payables" in r.text
    assert "Payables due soon" not in r.text
