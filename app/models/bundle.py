from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Bundle(Base):
    """A SKU sold on TikTok that is actually a bundle of physical SKUs."""
    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bundle_sku: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512))

    components: Mapped[list["BundleComponent"]] = relationship(
        back_populates="bundle", cascade="all, delete-orphan"
    )


class BundleComponent(Base):
    __tablename__ = "bundle_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bundle_id: Mapped[int] = mapped_column(ForeignKey("bundles.id"), index=True)
    component_sku: Mapped[str] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # If set, this fraction (0-1) of bundle revenue is allocated to this component
    # for SKU-level reporting. If null, allocation falls back to a uniform split
    # weighted by quantity * component COGS — see app/reports/sku_profitability.py.
    revenue_allocation_pct: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)

    bundle: Mapped[Bundle] = relationship(back_populates="components")
