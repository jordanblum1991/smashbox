"""Invoices issued by Outlandish for TikTok Shop management services.

Manually-created via /admin/invoices — no automation from P&L data. PDF
rendered via WeasyPrint from app/templates/invoices/invoice_pdf.html,
which is also the bare-HTML preview embedded in the detail page iframe.

`brand_code` is forward-compatible for the multi-tenant rebuild —
defaults to "SMASHBOX", unused in query scoping today.

Status values today: "issued", "paid". Stored as a plain string (not an
enum) so future states (e.g. "voided") can be added without a migration.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Human-issued identifier, e.g. "OL-2026-007". Unique across all
    # invoices; the create form pre-fills with max(suffix)+1 but is
    # editable so finance can issue out-of-band numbers when needed.
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    issue_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Multi-line address block as the user entered it on the form.
    # Defaults to Smashbox's address but is editable per invoice.
    bill_to_block: Mapped[str] = mapped_column(Text, nullable=False)

    # Single line-item content. Qty is always 1 and Tax is always $0.00,
    # so `amount` is unit price, subtotal, and total due simultaneously.
    description_headline: Mapped[str] = mapped_column(String(256), nullable=False)
    description_subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # "issued" or "paid".
    status: Mapped[str] = mapped_column(String(16), default="issued", nullable=False)

    # Multi-tenant placeholder; "SMASHBOX" always today.
    brand_code: Mapped[str] = mapped_column(
        String(32), default="SMASHBOX", nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
