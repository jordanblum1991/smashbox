"""On-hand inventory snapshots.

Phase A of the demand-planning module. Each row is "at time T, this SKU had
N units on hand." The planner reads the LATEST snapshot per SKU as the
starting point for days-of-cover and reorder-point math.

Keying: `(sku, captured_at)` is unique — re-uploading the same snapshot is a
no-op, but a new capture at a later timestamp creates a new row so we keep
a full history. We deliberately don't key by a synthetic ID alone because
the CSV operator owns timestamps.

Single-location only for now (per product-requirements answer). When the
warehouse setup adds multiple locations, add a `location` column to the
unique key without breaking existing rows.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("sku", "captured_at", name="uq_inventory_sku_captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    sku: Mapped[str] = mapped_column(String(128), index=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
