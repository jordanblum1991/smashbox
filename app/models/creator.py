"""Creator entity — thin join target for sample shipments and future per-creator analytics.

Deliberately minimal: handle, name, platform, brand, created_at only.
Richer fields (per-creator GMV, conversion rate, content links) are deferred
to a later phase; this row is the stable FK anchor everything else will join to.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (
        UniqueConstraint("shop_id", "handle", "platform", name="uq_creator_per_shop_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id"), index=True, nullable=True
    )
    # Multi-tenancy Phase 2a — same pattern as all other tenant-scoped tables.

    handle: Mapped[str] = mapped_column(String(256), index=True)
    # Primary lookup key — TikTok @handle, IG username, etc.
    # Unique per (shop_id, handle, platform) via table constraint above.

    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Display / legal name — optional; handle is the identifier.

    platform: Mapped[str] = mapped_column(String(64), default="unknown")
    # NOT NULL, defaults to "unknown". Required so the unique constraint on
    # (shop_id, handle, platform) actually enforces dedup — NULL values are
    # distinct in SQL unique constraints and would allow duplicate @handle rows.
    # "tiktok" | "instagram" | "unknown" are the expected values.

    brand: Mapped[str] = mapped_column(String(64), index=True)
    # Same field name/type as Order.brand / Sku.brand — multi-brand filter hook.
    # A second brand adds rows; no schema change required.

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    # Audit timestamp. No analytics fields until per-creator GMV is built.
