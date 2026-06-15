"""Payable reminders: the 'due soon' AP signal (compute + dashboard banner)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice
from app.reports.inventory_alerts import _reset_cache
from app.reports.overdue_ap import compute_due_soon_ap
from app.services.reporting_tz import today_local


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _reset_cache()  # the action_items nav count reads the inventory alert cache
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


def test_due_soon_surfaces_via_action_center(client: TestClient):
    with SessionLocal() as db:
        _inv(db, "SOON-7", 7, "456.00")
        db.commit()
    # Dashboard shows the consolidated entry banner...
    r = client.get("/")
    assert r.status_code == 200
    assert "Open Action Center" in r.text
    # ...and the Action Center details the due-soon payable.
    ac = client.get("/action-center")
    assert ac.status_code == 200
    assert "due soon" in ac.text
    assert "$456.00" in ac.text
    assert "within 14 days" in ac.text


def test_overdue_not_counted_as_due_soon(client: TestClient):
    # An overdue invoice is an "overdue" item, NOT a "due soon" one.
    with SessionLocal() as db:
        _inv(db, "OD", -5, "111.00")
        db.commit()
    ac = client.get("/action-center")
    assert ac.status_code == 200
    assert "overdue invoice" in ac.text
    assert "due soon" not in ac.text
