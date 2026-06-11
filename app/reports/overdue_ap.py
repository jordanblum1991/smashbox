"""Overdue accounts-payable signal — product (Smashbox) invoices that are past
their due date with an outstanding balance.

Surfaced two ways, mirroring the Data Health badge: a red count badge on the
"Invoices & AP" nav item and a dashboard banner. `PurchaseInvoice.is_overdue`
is the single source of truth (due_date in the past AND net_owed > 0), so this
stays in lock-step with the per-invoice flag shown on the Product Invoices page.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.purchase_invoice import PurchaseInvoice


def compute_overdue_ap(db: Session) -> dict:
    """Return {"count", "total"} for overdue product invoices. `total` is the
    sum of their outstanding balances (net_owed). Credits/payments are
    eager-loaded so net_owed/is_overdue don't trigger per-row queries."""
    invoices = db.execute(
        select(PurchaseInvoice).options(
            selectinload(PurchaseInvoice.credits),
            selectinload(PurchaseInvoice.payments),
        )
    ).scalars().all()
    overdue = [i for i in invoices if i.is_overdue]
    return {
        "count": len(overdue),
        "total": sum((i.net_owed for i in overdue), Decimal("0")),
    }
