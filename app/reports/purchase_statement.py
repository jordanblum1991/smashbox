"""Smashbox Product Invoices — account statement.

Builds a chronological transaction ledger across all invoices: each invoice is a
DEBIT (increases what we owe), each credit memo and each payment is a reduction.
Running balance = Σ debits − Σ credits − Σ payments. For a [start, end] window
(inclusive), everything dated before `start` rolls into the opening balance; the
closing balance is the balance through `end`. With no window, it's the full
history from a $0 opening balance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.purchase_invoice import PurchaseInvoice

_ZERO = Decimal("0")


@dataclass
class StatementRow:
    date: date
    description: str
    debit: Decimal      # invoice charge (0 otherwise)
    credit: Decimal     # credit memo (0 otherwise)
    payment: Decimal    # payment (0 otherwise)
    balance: Decimal    # running balance AFTER this row


@dataclass
class PurchaseStatement:
    opening_balance: Decimal
    rows: list[StatementRow] = field(default_factory=list)
    total_debits: Decimal = _ZERO
    total_credits: Decimal = _ZERO
    total_payments: Decimal = _ZERO
    closing_balance: Decimal = _ZERO
    start: date | None = None
    end: date | None = None


# Event sort order on the same day: invoice charge, then credits, then payments.
_INVOICE, _CREDIT, _PAYMENT = 0, 1, 2


def compute_purchase_statement(
    db: Session,
    start: date | None = None,
    end: date | None = None,
) -> PurchaseStatement:
    invoices = db.execute(
        select(PurchaseInvoice).options(
            selectinload(PurchaseInvoice.credits),
            selectinload(PurchaseInvoice.payments),
        )
    ).scalars().all()

    # Flatten everything into dated events: (date, order, number, debit, credit, payment, desc)
    events: list[tuple] = []
    for inv in invoices:
        events.append((inv.invoice_date, _INVOICE, inv.number,
                       inv.amount, _ZERO, _ZERO, f"Invoice {inv.number}"))
        for c in inv.credits:
            desc = f"Credit · {inv.number}" + (f" — {c.reason}" if c.reason else "")
            events.append((c.credit_date, _CREDIT, inv.number, _ZERO, c.amount, _ZERO, desc))
        for p in inv.payments:
            desc = f"Payment · {inv.number}" + (f" — {p.reference}" if p.reference else "")
            events.append((p.payment_date, _PAYMENT, inv.number, _ZERO, _ZERO, p.amount, desc))

    events.sort(key=lambda e: (e[0], e[1], e[2]))

    def _effect(debit, credit, payment) -> Decimal:
        return debit - credit - payment

    opening = _ZERO
    for ev in events:
        d = ev[0]
        if start is not None and d < start:
            opening += _effect(ev[3], ev[4], ev[5])

    stmt = PurchaseStatement(opening_balance=opening, start=start, end=end)
    balance = opening
    for ev in events:
        d, _order, _num, debit, credit, payment, desc = ev
        if start is not None and d < start:
            continue
        if end is not None and d > end:
            continue
        balance += _effect(debit, credit, payment)
        stmt.rows.append(StatementRow(date=d, description=desc, debit=debit,
                                      credit=credit, payment=payment, balance=balance))
        stmt.total_debits += debit
        stmt.total_credits += credit
        stmt.total_payments += payment

    stmt.closing_balance = balance
    return stmt
