"""Raw rows from TikTok settlement files — used for reconciliation against orders."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    statement_id: Mapped[str] = mapped_column(String(64), index=True)
    tiktok_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)  # sale, refund, fee, adjustment...

    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
