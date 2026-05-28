"""Manual ad credits — one row per (year, month), applied on a specific date.

TikTok occasionally issues promotional ad credits that don't always flow
through the standard Cost export. This table lets finance manually record
those credits so the P&L can show Net Ad Spend = Gross − Credits.

`applied_date` is the calendar date the credit is attributed to — what the
P&L filters on. `year`/`month` are kept in sync with `applied_date` on every
write so the existing `uq_ad_credits_year_month` UNIQUE constraint continues
to enforce "at most one credit per calendar month." The legacy columns are
dead reads for app code; only the UNIQUE constraint depends on them.

Saving any amount (including $0) records a confirmed entry that persists
across reloads — there is no "clear" action.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
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
    # applied_date is the source of truth for P&L windowing. Nullable on the
    # column so _ensure_columns can ADD COLUMN without a default; the boot
    # backfill populates existing rows and every write thereafter sets it.
    applied_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
