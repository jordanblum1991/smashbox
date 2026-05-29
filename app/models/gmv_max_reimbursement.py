"""Manual Smashbox-paid reimbursements for Outlandish GMV Max ad spend.

Smashbox reimburses Outlandish for GMV Max ad spend dollar-for-dollar; this
model lets finance manually record those reimbursements per (year, month) so
the P&L can offset the corresponding TikTok Ads (GMV Max) expense.

This is a SEPARATE pipeline from AdCredit (which captures TikTok-issued
platform credits). The two flows are fully independent — both can exist for
the same month, both feed their own P&L line, neither shadows the other.

Each entry is the explicit value confirmed by Smashbox (typically via email
or check). NEVER auto-default to the GMV Max amount; null = not yet entered
= no offset. The amount is stored positive; the P&L adds it back to reduce
net cost (same sign convention as AdCredit.amount → ad_credit_offset).

Edit-not-stack: UNIQUE on (year, month) means re-saving the same month
overwrites in place rather than appending.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class GmvMaxReimbursement(Base):
    __tablename__ = "gmv_max_reimbursements"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_gmv_max_reimbursements_year_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
