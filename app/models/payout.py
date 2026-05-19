"""TikTok payouts to our bank — the cash-side anchor for reconciliation."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    payout_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
