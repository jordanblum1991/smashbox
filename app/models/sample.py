"""Samples shipped to creators / for seeding.

No allowance comparison: Smashbox has no monthly sample limit, so the
`is_paid_oversample` flag is preserved only for rows that are explicitly billed
(set manually or by future importers). See app/reports/sample_tracking.py.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    shipped_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    creator_handle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_paid_oversample: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
