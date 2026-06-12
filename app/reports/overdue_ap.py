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
from app.services.reporting_tz import today_local

DUE_SOON_DAYS = 14


def _outstanding(db: Session) -> list[PurchaseInvoice]:
    """All product invoices with credits/payments eager-loaded (so net_owed /
    is_overdue don't trigger per-row queries)."""
    return list(db.execute(
        select(PurchaseInvoice).options(
            selectinload(PurchaseInvoice.credits),
            selectinload(PurchaseInvoice.payments),
        )
    ).scalars().all())


def compute_overdue_ap(db: Session) -> dict:
    """Return {"count", "total"} for overdue product invoices. `total` is the
    sum of their outstanding balances (net_owed)."""
    overdue = [i for i in _outstanding(db) if i.is_overdue]
    return {
        "count": len(overdue),
        "total": sum((i.net_owed for i in overdue), Decimal("0")),
    }


def compute_due_soon_ap(db: Session, within_days: int = DUE_SOON_DAYS) -> dict:
    """Return {"count", "total", "within_days"} for outstanding product invoices
    that are NOT yet overdue but come due within `within_days` — a proactive
    "pay these soon" reminder. Overdue invoices are excluded (the overdue signal
    covers those)."""
    today = today_local()
    soon = [
        i for i in _outstanding(db)
        if i.net_owed > 0 and i.due_date is not None
        and 0 <= (i.due_date - today).days <= within_days
    ]
    return {
        "count": len(soon),
        "total": sum((i.net_owed for i in soon), Decimal("0")),
        "within_days": within_days,
    }
