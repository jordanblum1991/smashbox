"""SKU alias mapping.

When a physical product is re-coded — e.g. legacy `C09D01` is renamed
`SBX-C09D01` — its demand and inventory history splits between two SKU
codes. Velocity, σ, and reorder math all assume one code = one product,
so the split signal undercounts both sides.

A `SkuAlias` row declares that `alias_sku` should be treated as
`canonical_sku` everywhere downstream. The mapping is at the
*string-identifier* level (because `OrderLine.sku`, `InventorySnapshot.sku`,
etc. all hold strings), not at the `Sku.id` FK level — that way a legacy
code with no `Sku` row can still be aliased.

Chains (A → B → C) are allowed; `app/services/sku_alias.load_alias_map`
resolves them to a flat `{alias: terminal_canonical}` dict at read time.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class SkuAlias(Base):
    __tablename__ = "sku_aliases"
    __table_args__ = (
        UniqueConstraint("shop_id", "alias_sku", name="uq_sku_alias_per_shop"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id"), index=True, nullable=True
    )

    # The SKU code that should be redirected.
    alias_sku: Mapped[str] = mapped_column(String(128), index=True)
    # The SKU code it redirects to (the "real" identifier going forward).
    canonical_sku: Mapped[str] = mapped_column(String(128), index=True)

    # Audit + provenance.
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
