"""Legacy per-(brand, year, month) sample allowance units. No active code path
writes here (the Creator Sample module tracks shipments, not allowances), but
historical rows exist. Modeled so the table is managed by migrations and its
data is preserved on the Postgres move. Schema matches the existing table
exactly: indexes on brand + year only (NOT month), UNIQUE(brand, year, month)."""
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
    month: Mapped[int] = mapped_column(Integer)            # not indexed (matches existing schema)
    allowance_units: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
