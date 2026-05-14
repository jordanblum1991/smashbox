"""Per-brand-per-month free-sample allowance.

One row per (brand, year, month). The sample-tracking report looks this up to
classify shipped sample units into "free (within allowance)" vs "paid oversample
(over allowance)". A `notes` column makes the source of each number auditable —
e.g. "per Q2 2026 creator program agreement".

Fallback: when no row exists for the requested (brand, year, month), the report
uses settings.free_sample_monthly_allowance and surfaces that source on the
page so the user knows.
"""
from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SampleAllowance(Base):
    __tablename__ = "sample_allowances"
    __table_args__ = (
        UniqueConstraint("brand", "year", "month", name="uq_allowance_brand_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer)
    allowance_units: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
