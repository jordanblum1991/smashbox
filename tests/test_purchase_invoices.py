"""Smashbox Product Invoices — inbound AP ledger. Header-only invoices with
credits tied to each, Open/Paid status, net owed = amount − credits. Routes are
called directly (same pattern as the other admin CRUD tests).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.purchase_invoice import PurchaseInvoice
from app.routers.purchase_invoices import (
    add_purchase_credit,
    add_purchase_payment,
    create_purchase_invoice,
    delete_purchase_credit,
    delete_purchase_invoice,
    delete_purchase_payment,
    set_purchase_invoice_status,
    update_purchase_credit,
    update_purchase_invoice,
    update_purchase_payment,
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
        number=number, invoice_date="2026-06-01", amount=amount, due_date=None, note="PO 42", db=db
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
        r1 = create_purchase_invoice(number="A", invoice_date="2026-06-01", amount="abc", due_date=None, note=None, db=db)
        r2 = create_purchase_invoice(number="B", invoice_date="2026-06-01", amount="0", due_date=None, note=None, db=db)
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


def test_edit_invoice_updates_fields():
    with SessionLocal() as db:
        _mk(db, number="SBX-001"); db.commit()
        inv_id = _invoices(db)[0].id
        update_purchase_invoice(invoice_id=inv_id, number="SBX-002",
                                invoice_date="2026-07-01", amount="1500.00", due_date=None, note="rev", db=db)
        db.commit()
        inv = _invoices(db)[0]
        assert inv.number == "SBX-002"
        assert inv.amount == Decimal("1500.00")
        assert inv.note == "rev"


def test_edit_invoice_keep_same_number_ok():
    with SessionLocal() as db:
        _mk(db, number="SBX-001"); db.commit()
        inv_id = _invoices(db)[0].id
        r = update_purchase_invoice(invoice_id=inv_id, number="SBX-001",
                                    invoice_date="2026-06-01", amount="1200.00", due_date=None, note=None, db=db)
        db.commit()
        assert r.status_code == 303 and "error=" not in str(r.headers.get("location"))
        assert _invoices(db)[0].amount == Decimal("1200.00")


def test_edit_invoice_duplicate_number_rejected():
    with SessionLocal() as db:
        _mk(db, number="SBX-001"); _mk(db, number="SBX-002"); db.commit()
        target = [i for i in _invoices(db) if i.number == "SBX-002"][0]
        r = update_purchase_invoice(invoice_id=target.id, number="SBX-001",
                                    invoice_date="2026-06-01", amount="1000.00", due_date=None, note=None, db=db)
        db.commit()
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert [i for i in _invoices(db) if i.id == target.id][0].number == "SBX-002"


def test_edit_invoice_invalid_amount_rejected():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        r = update_purchase_invoice(invoice_id=inv_id, number="SBX-001",
                                    invoice_date="2026-06-01", amount="0", due_date=None, note=None, db=db)
        db.commit()
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert _invoices(db)[0].amount == Decimal("1000.00")


def test_edit_invoice_404():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            update_purchase_invoice(invoice_id=999, number="X", invoice_date="2026-06-01",
                                    amount="10.00", due_date=None, note=None, db=db)
        assert ei.value.status_code == 404


def test_edit_credit_updates_fields():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_credit(invoice_id=inv.id, credit_date="2026-06-05", amount="200.00", reason="x", db=db)
        db.commit()
        cid = _invoices(db)[0].credits[0].id
        update_purchase_credit(invoice_id=inv.id, credit_id=cid, credit_date="2026-06-07",
                               amount="250.00", reason="adjusted", db=db)
        db.commit()
        c = _invoices(db)[0].credits[0]
        assert c.amount == Decimal("250.00")
        assert c.reason == "adjusted"
        assert _invoices(db)[0].net_owed == Decimal("750.00")


def test_edit_credit_404():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        with pytest.raises(HTTPException) as ei:
            update_purchase_credit(invoice_id=inv_id, credit_id=999, credit_date="2026-06-05",
                                   amount="50.00", reason=None, db=db)
        assert ei.value.status_code == 404


def test_payments_reduce_net_owed():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        add_purchase_credit(invoice_id=inv_id, credit_date="2026-06-05", amount="200.00", reason=None, db=db)
        add_purchase_payment(invoice_id=inv_id, payment_date="2026-06-10", amount="300.00", reference="ck 88", db=db)
        db.commit()
        inv = _invoices(db)[0]
        assert inv.payments_total == Decimal("300.00")
        assert inv.net_owed == Decimal("500.00")   # 1000 − 200 credit − 300 payment


def test_edit_payment_and_delete():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_payment(invoice_id=inv.id, payment_date="2026-06-10", amount="300.00", reference=None, db=db)
        db.commit()
        pid = _invoices(db)[0].payments[0].id
        update_purchase_payment(invoice_id=inv.id, payment_id=pid, payment_date="2026-06-11",
                                amount="350.00", reference="rev", db=db)
        db.commit()
        assert _invoices(db)[0].net_owed == Decimal("650.00")   # 1000 − 350
        delete_purchase_payment(invoice_id=inv.id, payment_id=pid, db=db); db.commit()
        assert _invoices(db)[0].net_owed == Decimal("1000.00")


def test_payment_invalid_amount_rejected():
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv_id = _invoices(db)[0].id
        r = add_purchase_payment(invoice_id=inv_id, payment_date="2026-06-10", amount="0", reference=None, db=db)
        db.commit()
        assert r.status_code == 303 and "error=" in str(r.headers.get("location"))
        assert _invoices(db)[0].payments == []


def test_due_date_create_edit_and_clear():
    with SessionLocal() as db:
        create_purchase_invoice(number="D1", invoice_date="2026-06-01", amount="100.00",
                                due_date="2026-07-01", note=None, db=db)
        db.commit()
        inv = _invoices(db)[0]
        assert inv.due_date == date(2026, 7, 1)
        # Edit to a new due date, then clear it (blank → None).
        update_purchase_invoice(invoice_id=inv.id, number="D1", invoice_date="2026-06-01",
                                amount="100.00", due_date="2026-07-15", note=None, db=db)
        db.commit()
        assert _invoices(db)[0].due_date == date(2026, 7, 15)
        update_purchase_invoice(invoice_id=inv.id, number="D1", invoice_date="2026-06-01",
                                amount="100.00", due_date="", note=None, db=db)
        db.commit()
        assert _invoices(db)[0].due_date is None


def test_create_without_due_date_is_none():
    with SessionLocal() as db:
        _mk(db); db.commit()
        assert _invoices(db)[0].due_date is None


def test_is_overdue():
    with SessionLocal() as db:
        # Past due with a balance → overdue.
        create_purchase_invoice(number="OD", invoice_date="2026-01-01", amount="1000.00",
                                due_date="2026-01-15", note=None, db=db)
        db.commit()
        inv = _invoices(db)[0]
        assert inv.is_overdue is True
        # Pay it off → no longer overdue (net owed 0).
        add_purchase_payment(invoice_id=inv.id, payment_date="2026-02-01", amount="1000.00", reference=None, db=db)
        db.commit()
        assert _invoices(db)[0].is_overdue is False
        # Future due date → not overdue.
        create_purchase_invoice(number="FUT", invoice_date="2026-06-01", amount="500.00",
                                due_date="2999-01-01", note=None, db=db)
        # No due date → not overdue.
        create_purchase_invoice(number="ND", invoice_date="2026-06-01", amount="500.00",
                                due_date=None, note=None, db=db)
        db.commit()
        by_num = {i.number: i for i in _invoices(db)}
        assert by_num["FUT"].is_overdue is False
        assert by_num["ND"].is_overdue is False


def test_payment_on_missing_invoice_404():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            add_purchase_payment(invoice_id=999, payment_date="2026-06-10", amount="50.00", reference=None, db=db)
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


def test_statement_page_renders(client):
    with SessionLocal() as db:
        _mk(db); db.commit()
        inv = _invoices(db)[0]
        add_purchase_credit(invoice_id=inv.id, credit_date="2026-06-05", amount="200.00", reason=None, db=db)
        add_purchase_payment(invoice_id=inv.id, payment_date="2026-06-10", amount="300.00", reference="ck 9", db=db)
        db.commit()
    r = client.get("/admin/product-invoices/statement")
    assert r.status_code == 200
    assert "Statement" in r.text
    assert "Opening balance" in r.text
    assert "Invoice SBX-001" in r.text
    assert "$500.00" in r.text          # closing balance 1000 − 200 − 300


def test_statement_export_downloads(client):
    with SessionLocal() as db:
        _mk(db); db.commit()
    r = client.get("/export/product-invoice-statement.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert len(r.content) > 100
