"""Smashbox Product Invoices — inbound AP ledger. Header-only invoices with
credits tied to each, Open/Paid status, net owed = amount − credits. Routes are
called directly (same pattern as the other admin CRUD tests).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice
from app.routers.purchase_invoices import (
    add_purchase_credit,
    create_purchase_invoice,
    delete_purchase_credit,
    delete_purchase_invoice,
    set_purchase_invoice_status,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _invoices(db):
    return db.query(PurchaseInvoice).order_by(PurchaseInvoice.id).all()


def _mk(db, number="SBX-001", amount="1000.00"):
    return create_purchase_invoice(
        number=number, invoice_date="2026-06-01", amount=amount, note="PO 42", db=db
    )


def test_create_invoice():
    with SessionLocal() as db:
        r = _mk(db)
        db.commit()
        assert r.status_code == 303
        inv = _invoices(db)[0]
        assert inv.number == "SBX-001"
        assert inv.amount == Decimal("1000.00")
        assert inv.status == "open"
        assert inv.note == "PO 42"
        assert inv.net_owed == Decimal("1000.00")


def test_credits_reduce_net_owed():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        add_purchase_credit(invoice_id=inv_id, credit_date="2026-06-05", amount="200.00", reason="damaged", db=db)
        add_purchase_credit(invoice_id=inv_id, credit_date="2026-06-06", amount="300.00", reason=None, db=db)
        db.commit()
        inv = _invoices(db)[0]
        assert inv.credits_total == Decimal("500.00")
        assert inv.net_owed == Decimal("500.00")


def test_invalid_amount_rejected_no_write():
    with SessionLocal() as db:
        r1 = create_purchase_invoice(number="A", invoice_date="2026-06-01", amount="abc", note=None, db=db)
        r2 = create_purchase_invoice(number="B", invoice_date="2026-06-01", amount="0", note=None, db=db)
        db.commit()
        assert r1.status_code == 303 and "error=" in str(r1.headers.get("location"))
        assert r2.status_code == 303 and "error=" in str(r2.headers.get("location"))
        assert _invoices(db) == []


def test_duplicate_number_rejected():
    with SessionLocal() as db:
        _mk(db, number="SBX-001"); db.commit()
        r = _mk(db, number="SBX-001"); db.commit()
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert len(_invoices(db)) == 1


def test_mark_paid_and_reopen():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        set_purchase_invoice_status(invoice_id=inv_id, status="paid", db=db); db.commit()
        assert _invoices(db)[0].status == "paid"
        set_purchase_invoice_status(invoice_id=inv_id, status="open", db=db); db.commit()
        assert _invoices(db)[0].status == "open"


def test_delete_credit_restores_net():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_credit(invoice_id=inv.id, credit_date="2026-06-05", amount="200.00", reason=None, db=db)
        db.commit()
        cid = _invoices(db)[0].credits[0].id
        delete_purchase_credit(invoice_id=inv.id, credit_id=cid, db=db); db.commit()
        assert _invoices(db)[0].net_owed == Decimal("1000.00")
        assert _invoices(db)[0].credits == []


def test_delete_invoice_cascades_credits():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_credit(invoice_id=inv.id, credit_date="2026-06-05", amount="200.00", reason=None, db=db)
        db.commit()
        from app.models.purchase_invoice import PurchaseInvoiceCredit
        assert db.query(PurchaseInvoiceCredit).count() == 1
        delete_purchase_invoice(invoice_id=inv.id, db=db); db.commit()
        assert _invoices(db) == []
        assert db.query(PurchaseInvoiceCredit).count() == 0   # cascade


def test_credit_on_missing_invoice_404():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            add_purchase_credit(invoice_id=999, credit_date="2026-06-05", amount="50.00", reason=None, db=db)
        assert ei.value.status_code == 404


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_view_renders(client):
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_credit(invoice_id=inv.id, credit_date="2026-06-05", amount="200.00", reason="damaged units", db=db)
        db.commit()
    r = client.get("/admin/product-invoices")
    assert r.status_code == 200
    assert "Smashbox Product Invoices" in r.text
    assert "SBX-001" in r.text
    assert "Net Owed" in r.text
    assert "$800.00" in r.text          # 1000 − 200
    assert "damaged units" in r.text


def test_list_view_empty_state(client):
    r = client.get("/admin/product-invoices")
    assert r.status_code == 200
    assert "No invoices logged yet" in r.text
