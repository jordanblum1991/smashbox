"""Overdue accounts-payable signal: compute helper + dashboard banner."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoicePayment
from app.reports.overdue_ap import compute_overdue_ap
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


def test_compute_overdue_ap():
    with SessionLocal() as db:
        _inv(db, "OD-PASTDUE", -5, "1000.00")          # overdue: past due, open
        _inv(db, "OD-FUTURE", 5, "500.00")             # not overdue: due in future
        _inv(db, "OD-PAID", -10, "300.00", status="paid")  # past due but fully paid
        db.commit()
        paid = db.execute(
            select(PurchaseInvoice).where(PurchaseInvoice.number == "OD-PAID")
        ).scalar_one()
        db.add(PurchaseInvoicePayment(
            purchase_invoice_id=paid.id,
            payment_date=today_local() - timedelta(days=8),
            amount=Decimal("300.00"),                  # net_owed -> 0 -> not overdue
        ))
        db.commit()
        result = compute_overdue_ap(db)
    assert result["count"] == 1
    assert result["total"] == Decimal("1000.00")


def test_dashboard_shows_overdue_banner(client: TestClient):
    with SessionLocal() as db:
        _inv(db, "OD-PASTDUE", -3, "750.00")
        db.commit()
    r = client.get("/")
    assert r.status_code == 200
    assert "Overdue payables" in r.text
    assert "$750.00" in r.text


def test_dashboard_no_banner_when_none(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "Overdue payables" not in r.text
