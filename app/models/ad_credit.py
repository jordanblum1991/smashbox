"""Manual ad credits — one row per (year, month).

TikTok occasionally issues promotional ad credits that don't always flow
through the standard Cost export. This table lets finance manually record
those credits so the P&L can show Net Ad Spend = Gross − Credits.

The row is upserted by (year, month); zero or null amount → row is deleted.
A free-text `note` field captures *why* the credit was issued
(e.g. "Welcome promo", "Make-good for outage 2026-03-04") for audit.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class AdCredit(Base):
    __tablename__ = "ad_credits"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_ad_credits_year_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
