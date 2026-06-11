"""CSV export endpoints for the Vendor and Product invoice lists."""
import csv
import io
from datetime import date
from decimal import Decimal
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.invoice import Invoice
from app.models.purchase_invoice import PurchaseInvoice


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_vendor_invoices_csv(client: TestClient):
    with SessionLocal() as db:
        db.add(Invoice(
            number="OL-2026-009", issue_date=date(2026, 6, 1),
            bill_to_block="Smashbox", description_headline="June, mgmt services",
            amount=Decimal("1234.56"), status="issued",
        ))
        db.commit()
    r = client.get("/admin/invoices.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Number", "Issue Date", "Description", "Amount", "Status"]
    # The comma in the description must survive (csv quoting).
    assert ["OL-2026-009", "2026-06-01", "June, mgmt services", "1234.56", "issued"] in rows


def test_product_invoices_csv(client: TestClient):
    with SessionLocal() as db:
        db.add(PurchaseInvoice(
            number="SBX-900", invoice_date=date(2026, 6, 1),
            amount=Decimal("1000.00"), status="open",
        ))
        db.commit()
    r = client.get("/admin/product-invoices.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == [
        "Number", "Invoice Date", "Due Date", "Amount",
        "Credits", "Payments", "Net Owed", "Status",
    ]
    row = next(r for r in rows[1:] if r[0] == "SBX-900")
    assert row[3] == "1000.00"      # amount
    assert row[6] == "1000.00"      # net owed (no credits/payments)
    assert row[7] == "open"


def test_product_invoices_csv_import(client: TestClient):
    content = (
        "Number,Invoice Date,Amount,Due Date,Note,Status\n"
        "SBX-IMP-1,2026-06-01,500.00,2026-07-01,PO 9,open\n"
        "SBX-IMP-2,2026-06-02,250.00,,,paid\n"          # blank due date -> Net 30
        "SBX-IMP-1,2026-06-03,999.00,,,open\n"          # duplicate number -> skipped
        ",2026-06-04,100.00,,,open\n"                   # missing number -> error
    )
    r = client.post(
        "/admin/product-invoices/import-csv",
        files={"file": ("invoices.csv", content, "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = unquote_plus(r.headers["location"])
    assert loc.startswith("/admin/invoices?tab=product")
    assert "Imported 2 invoices" in loc          # 2 created, 1 dup, 1 error
    with SessionLocal() as db:
        rows = db.execute(select(PurchaseInvoice)).scalars().all()
        assert {i.number for i in rows} == {"SBX-IMP-1", "SBX-IMP-2"}
        paid = db.execute(
            select(PurchaseInvoice).where(PurchaseInvoice.number == "SBX-IMP-2")
        ).scalar_one()
        assert paid.status == "paid"
        assert paid.due_date is not None          # Net 30 default applied
