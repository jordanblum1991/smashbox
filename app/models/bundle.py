"""Bundles — TikTok SKUs that are actually multiple physical SKUs packaged together.

Each Bundle row has a primary TikTok identifier (`tiktok_sku_id`) and an
SBX-style `bundle_sku` mirroring the Sku table convention. Components are
listed per bundle with quantity, MSRP and COGS so a bundle's true cost can be
derived by summing its components.
"""
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Bundle(Base):
    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    # Either may be missing in the mapping file — at least one must be set.
    bundle_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    tiktok_sku_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    name: Mapped[str] = mapped_column(String(512))
    variation: Mapped[str | None] = mapped_column(String(256), nullable=True)
    brand: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(String(16), default="Active")

    msrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    components: Mapped[list["BundleComponent"]] = relationship(
        back_populates="bundle", cascade="all, delete-orphan"
    )

    @property
    def calculated_cogs(self) -> Decimal:
        return sum((c.quantity * c.unit_cogs for c in self.components), Decimal("0"))

    @property
    def calculated_msrp(self) -> Decimal:
        return sum((c.quantity * c.msrp for c in self.components), Decimal("0"))


class BundleComponent(Base):
    __tablename__ = "bundle_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bundle_id: Mapped[int] = mapped_column(ForeignKey("bundles.id"), index=True)

    component_sku: Mapped[str] = mapped_column(String(128), index=True)
    component_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    msrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    unit_cogs: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    bundle: Mapped["Bundle"] = relationship(back_populates="components")
