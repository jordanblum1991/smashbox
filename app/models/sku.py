"""SKU master.

A Sku row carries THREE keys that all identify the same physical SKU, because
the TikTok orders file and settlement file each use different ones:

  - `sku`            — canonical SBX-form, e.g. "SBX-C00101" (`TikTok Shop SKU`)
  - `tiktok_alt_sku` — short C-form,        e.g. "C00101"     (`TikTok ALT SKU`)
  - `tiktok_sku_id`  — numeric ID,          e.g. "1729492097758368939"

The SKU resolver in app/services/sku_resolver.py joins OrderLine.sku against
any of these three so order lines find their canonical Sku regardless of which
key TikTok exported.
"""
from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sku(Base):
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Canonical key — preferred SBX-prefixed form.
    sku: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    # Alternate keys TikTok may use in exports. Nullable because not every SKU
    # has every form populated in the master sheet yet.
    tiktok_alt_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    tiktok_sku_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str | None] = mapped_column(String(256), nullable=True)
    item_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    msrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    unit_cogs: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
