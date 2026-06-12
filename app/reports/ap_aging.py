"""Accounts-payable aging — outstanding Smashbox product invoices bucketed by
how far past due they are (Current / 1-30 / 31-60 / 61-90 / 90+ days).

Computed off the same `PurchaseInvoice.net_owed` (amount − credits − payments)
and `due_date` as the overdue badge, so the two always agree. Only invoices
with an outstanding balance (net_owed > 0) appear; paid/credited invoices drop
out. An invoice with no due date is treated as Current (can't be past due).
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.purchase_invoice import PurchaseInvoice
from app.services.reporting_tz import today_local

BUCKET_LABELS = ["Current", "1-30", "31-60", "61-90", "90+"]


def bucket_for(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "Current"
    if days_past_due <= 30:
        return "1-30"
    if days_past_due <= 60:
        return "31-60"
    if days_past_due <= 90:
        return "61-90"
    return "90+"


@dataclass
class AgingInvoice:
    number: str
    invoice_date: date
    due_date: date | None
    net_owed: Decimal
    days_past_due: int          # <= 0 means not yet due
    bucket: str

    @property
    def days_until_due(self) -> int | None:
        """Days remaining until the due date for a not-yet-due invoice
        (0 = due today). None once it's past due — `days_past_due` covers that."""
        return None if self.days_past_due > 0 else -self.days_past_due


@dataclass
class AgingBucket:
    label: str
    count: int
    total: Decimal


@dataclass
class APAging:
    as_of: date
    buckets: list[AgingBucket] = field(default_factory=list)
    invoices: list[AgingInvoice] = field(default_factory=list)  # outstanding, most-overdue first

    @property
    def grand_total(self) -> Decimal:
        return sum((b.total for b in self.buckets), Decimal("0"))

    @property
    def total_count(self) -> int:
        return sum(b.count for b in self.buckets)

    @property
    def overdue_total(self) -> Decimal:
        return sum((b.total for b in self.buckets if b.label != "Current"), Decimal("0"))

    @property
    def overdue_count(self) -> int:
        return sum(b.count for b in self.buckets if b.label != "Current")


def compute_ap_aging(db: Session, as_of: date | None = None) -> APAging:
    today = as_of or today_local()
    rows = db.execute(
        select(PurchaseInvoice).options(
            selectinload(PurchaseInvoice.credits),
            selectinload(PurchaseInvoice.payments),
        )
    ).scalars().all()

    invoices: list[AgingInvoice] = []
    for inv in rows:
        if inv.net_owed <= 0:                    # nothing outstanding
            continue
        days = 0 if inv.due_date is None else (today - inv.due_date).days
        invoices.append(AgingInvoice(
            number=inv.number,
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
            net_owed=inv.net_owed,
            days_past_due=days,
            bucket=bucket_for(days),
        ))
    invoices.sort(key=lambda a: a.days_past_due, reverse=True)   # most overdue first

    buckets = []
    for label in BUCKET_LABELS:
        items = [a for a in invoices if a.bucket == label]
        buckets.append(AgingBucket(
            label=label,
            count=len(items),
            total=sum((a.net_owed for a in items), Decimal("0")),
        ))

    return APAging(as_of=today, buckets=buckets, invoices=invoices)
