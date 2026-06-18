"""Ad budget tracking — a Smashbox-allocated ad budget and the running spend
against it.

`AdBudget` is one budget period (a flexible date range + amount). Actual spend
is NOT stored here — it auto-pulls from the already-tracked daily GMV-Max ad
cost (`GmvMaxDailyMetric`) over the budget's date range (see
`app/reports/ad_budget.py`). `AdBudgetPromotion` is a manual, dated carve-out
that also reduces the budget's available balance from its date.

Manual entry via /admin/ad-budget. Purely a planning/reporting record — it does
NOT feed the P&L (the P&L reads actual ad spend directly).
"""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AdBudget(Base):
    __tablename__ = "ad_budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    label: Mapped[str] = mapped_column(String(64), nullable=False)        # "July 2026" / "Q3 2026"
    start_date: Mapped[date] = mapped_column(Date, nullable=False)        # inclusive
    end_date: Mapped[date] = mapped_column(Date, nullable=False)          # inclusive
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # allocated budget, > 0

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    promotions: Mapped[list["AdBudgetPromotion"]] = relationship(
        back_populates="budget",
        cascade="all, delete-orphan",
        order_by="AdBudgetPromotion.promo_date",
    )


class AdBudgetPromotion(Base):
    __tablename__ = "ad_budget_promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_budget_id: Mapped[int] = mapped_column(
        ForeignKey("ad_budgets.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)   # carved out, > 0
    promo_date: Mapped[date] = mapped_column(Date, nullable=False)            # reduces available from here
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    budget: Mapped["AdBudget"] = relationship(back_populates="promotions")
