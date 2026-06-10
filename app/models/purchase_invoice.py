"""Smashbox Product Invoices — an accounts-payable ledger of invoices RECEIVED
from Smashbox for sellable-inventory purchases, plus credits applied against
them.

This is the inbound counterpart to `app/models/invoice.py` (which is OUTBOUND —
invoices Outlandish issues to Smashbox for management services). Kept as a
separate model on purpose: different direction, different fields, no PDF/number
generation. Purely a financial record — it does NOT feed the P&L/COGS (COGS is
recognized per unit sold from SKU snapshots) and is not inventory-quantity
tracking (that's deferred to SAP).

Each invoice carries many credits (credit memos from Smashbox — damaged/returned
product, price adjustments). Net owed = amount − Σ credits. Manual entry via
/admin/product-invoices; mirrors the GMV Max Reimbursements admin pattern.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class PurchaseInvoice(Base):
    __tablename__ = "purchase_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    # Smashbox's invoice number. Unique so the same invoice isn't logged twice.
    number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)   # total billed
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open | paid
    note: Mapped[str | None] = mapped_column(Text, nullable=True)             # PO / memo

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    credits: Mapped[list["PurchaseInvoiceCredit"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="PurchaseInvoiceCredit.credit_date",
    )

    @property
    def credits_total(self) -> Decimal:
        return sum((c.amount for c in self.credits), Decimal("0"))

    @property
    def net_owed(self) -> Decimal:
        """Amount billed minus all credits applied. May go negative if credits
        exceed the invoice (surfaced as a flag in the UI, not blocked)."""
        return self.amount - self.credits_total


class PurchaseInvoiceCredit(Base):
    __tablename__ = "purchase_invoice_credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_invoice_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_invoices.id"), index=True, nullable=False
    )
    credit_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    invoice: Mapped["PurchaseInvoice"] = relationship(back_populates="credits")
