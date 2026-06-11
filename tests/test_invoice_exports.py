"""CSV export endpoints for the Vendor and Product invoice lists."""
import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

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
