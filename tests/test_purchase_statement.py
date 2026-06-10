"""Product-invoice account statement: chronological ledger with running balance,
opening balance for windowed periods, and same-day ordering (invoice → credit →
payment).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.purchase_invoice import (
    PurchaseInvoice,
    PurchaseInvoiceCredit,
    PurchaseInvoicePayment,
)
from app.reports.purchase_statement import compute_purchase_statement


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _inv(db, number, d, amount):
    inv = PurchaseInvoice(number=number, invoice_date=d, amount=Decimal(str(amount)), status="open")
    db.add(inv); db.flush()
    return inv


def _credit(db, inv, d, amount):
    db.add(PurchaseInvoiceCredit(purchase_invoice_id=inv.id, credit_date=d, amount=Decimal(str(amount))))
    db.flush()


def _pay(db, inv, d, amount):
    db.add(PurchaseInvoicePayment(purchase_invoice_id=inv.id, payment_date=d, amount=Decimal(str(amount))))
    db.flush()


def test_all_time_statement_running_balance():
    with SessionLocal() as db:
        a = _inv(db, "A", date(2026, 1, 10), "1000.00")
        _credit(db, a, date(2026, 1, 15), "200.00")
        _pay(db, a, date(2026, 2, 1), "300.00")
        _inv(db, "B", date(2026, 2, 5), "500.00")
        db.commit()
        s = compute_purchase_statement(db)
    assert s.opening_balance == Decimal("0")
    balances = [(r.date, r.balance) for r in s.rows]
    assert balances == [
        (date(2026, 1, 10), Decimal("1000.00")),   # +1000 invoice A
        (date(2026, 1, 15), Decimal("800.00")),    # −200 credit
        (date(2026, 2, 1), Decimal("500.00")),     # −300 payment
        (date(2026, 2, 5), Decimal("1000.00")),    # +500 invoice B
    ]
    assert s.total_debits == Decimal("1500.00")
    assert s.total_credits == Decimal("200.00")
    assert s.total_payments == Decimal("300.00")
    assert s.closing_balance == Decimal("1000.00")


def test_window_rolls_prior_into_opening_balance():
    with SessionLocal() as db:
        a = _inv(db, "A", date(2026, 1, 10), "1000.00")
        _credit(db, a, date(2026, 1, 15), "200.00")
        _inv(db, "B", date(2026, 2, 5), "500.00")
        db.commit()
        s = compute_purchase_statement(db, start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert s.opening_balance == Decimal("800.00")     # 1000 − 200 before the window
    assert len(s.rows) == 1                            # only invoice B is in-window
    assert s.rows[0].description == "Invoice B"
    assert s.rows[0].balance == Decimal("1300.00")
    assert s.total_debits == Decimal("500.00")
    assert s.closing_balance == Decimal("1300.00")


def test_same_day_ordering_invoice_credit_payment():
    with SessionLocal() as db:
        a = _inv(db, "A", date(2026, 1, 10), "1000.00")
        _pay(db, a, date(2026, 1, 10), "50.00")
        _credit(db, a, date(2026, 1, 10), "100.00")
        db.commit()
        s = compute_purchase_statement(db)
    kinds = [(r.description.split(" ")[0], r.balance) for r in s.rows]
    assert kinds == [
        ("Invoice", Decimal("1000.00")),   # invoice first
        ("Credit", Decimal("900.00")),     # then credit
        ("Payment", Decimal("850.00")),    # then payment
    ]


def test_empty_statement():
    with SessionLocal() as db:
        s = compute_purchase_statement(db)
    assert s.rows == []
    assert s.opening_balance == Decimal("0")
    assert s.closing_balance == Decimal("0")
