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

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sku(Base):
    """One row per TikTok variant.

    The unique key is `tiktok_sku_id` (when present) — TikTok issues a separate
    ID per variation, so a single TikTok Shop SKU (`sku`, the SBX-form) can map
    to multiple Sku rows, one per variation. `sku` is therefore NOT unique here;
    it is the human-readable product-family code shared across variations.

    Rows without a tiktok_sku_id (products not yet listed on TikTok) are still
    permitted — they are keyed by sku for upsert purposes via the importer.
    """
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    # Human-readable product code (SBX-form). Multiple rows can share this when
    # a single product has multiple TikTok variations.
    sku: Mapped[str] = mapped_column(String(128), index=True)

    # Canonical product identifier on TikTok. Unique across the table when set;
    # null only for SKUs not yet listed on TikTok.
    tiktok_alt_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    tiktok_sku_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)

    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str | None] = mapped_column(String(256), nullable=True)
    item_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Optional manual family label. When set, the inventory report groups this SKU
    # under it, overriding the auto code-base family rule (which misses lines whose
    # shade isn't a trailing 2-digit suffix). Blank = use the auto rule.
    family: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    msrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    unit_cogs: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Procurement attributes — Phase A of demand planning. Nullable because
    # they're populated per-SKU by the buyer over time, not all at once.
    # Effective defaults are applied at planner-compute time, not here, so
    # changing a global default doesn't require a DB migration.
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    case_pack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    safety_stock_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    is_reorderable: Mapped[bool] = mapped_column(Boolean, default=True)

    # Service-level tier for variance-based safety stock. Stored as a
    # fraction (0.95 = 95%), nullable, falls back to the global default
    # `settings.demand_service_level_default` at planner-compute time.
    # See `app/services/demand/replenishment.py` for how it's used.
    service_level: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
