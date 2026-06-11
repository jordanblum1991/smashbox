"""AP aging report: bucketing compute + page/CSV routes."""
import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoicePayment
from app.reports.ap_aging import compute_ap_aging
from app.services.reporting_tz import today_local


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _inv(db, number, due_offset_days, amount, status="open", payment=None):
    today = today_local()
    inv = PurchaseInvoice(
        number=number,
        invoice_date=today - timedelta(days=130),
        due_date=today + timedelta(days=due_offset_days),
        amount=Decimal(amount),
        status=status,
    )
    db.add(inv)
    db.flush()
    if payment is not None:
        db.add(PurchaseInvoicePayment(
            purchase_invoice_id=inv.id, payment_date=today, amount=Decimal(payment),
        ))
    return inv


def test_compute_ap_aging_buckets():
    with SessionLocal() as db:
        _inv(db, "CUR", 10, "100.00")                       # due in future -> Current
        _inv(db, "B1", -15, "200.00")                       # 15 days -> 1-30
        _inv(db, "B2", -45, "300.00")                       # 45 -> 31-60
        _inv(db, "B3", -75, "400.00")                       # 75 -> 61-90
        _inv(db, "B4", -120, "500.00")                      # 120 -> 90+
        _inv(db, "PAID", -200, "600.00", status="paid", payment="600.00")  # net 0 -> excluded
        db.commit()
        ag = compute_ap_aging(db)

    by = {b.label: b for b in ag.buckets}
    assert by["Current"].count == 1 and by["Current"].total == Decimal("100.00")
    assert by["1-30"].total == Decimal("200.00")
    assert by["31-60"].total == Decimal("300.00")
    assert by["61-90"].total == Decimal("400.00")
    assert by["90+"].total == Decimal("500.00")
    assert ag.grand_total == Decimal("1500.00")             # paid invoice excluded
    assert ag.overdue_total == Decimal("1400.00")           # excludes Current
    assert ag.overdue_count == 4
    assert len(ag.invoices) == 5
    # sorted most-overdue first
    assert ag.invoices[0].number == "B4"


def test_aging_page_renders(client: TestClient):
    with SessionLocal() as db:
        _inv(db, "OD-AGE", -45, "321.00")
        db.commit()
    r = client.get("/admin/product-invoices/aging")
    assert r.status_code == 200
    assert "Aging" in r.text
    assert "OD-AGE" in r.text
    assert "31-60" in r.text


def test_aging_csv(client: TestClient):
    with SessionLocal() as db:
        _inv(db, "OD-AGE", -45, "321.00")
        db.commit()
    r = client.get("/admin/product-invoices/aging.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Number", "Invoice Date", "Due Date", "Net Owed", "Days Past Due", "Bucket"]
    row = next(r for r in rows[1:] if r[0] == "OD-AGE")
    assert row[3] == "321.00"
    assert row[5] == "31-60"
