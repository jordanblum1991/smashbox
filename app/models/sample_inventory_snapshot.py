"""On-hand snapshots for the SAMPLE pool (the SBS warehouse in the SAP feed).

Deliberately a SEPARATE table from the sellable `InventorySnapshot` — sample and
sellable inventory must never be mixed (the demand planner reads sellable only;
this report reads samples only). Same shape otherwise: one row = "at time T, this
SKU had N sample units on hand", keyed unique on (sku, captured_at) so a same-day
re-sync upserts in place.

Fed by the SAP inventory sync (`app/services/inventory_sync.py`, SBS warehouse);
read by the sample-inventory report (`app/reports/sample_inventory.py`).
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SampleInventorySnapshot(Base):
    __tablename__ = "sample_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("sku", "captured_at", name="uq_sample_inventory_sku_captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    sku: Mapped[str] = mapped_column(String(128), index=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
