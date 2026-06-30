"""Sample inbound orders — incoming sample stock we're expecting into the SBS
(sample) warehouse.

Lightweight, supply-side counterpart to outbound `Sample` shipments. While an
order is OPEN it counts as "inbound (on order)" sample inventory; marking it
RECEIVED clears it — SAP's SBS warehouse picks the stock up as on-hand at that
point, so inbound never double-counts the SAP-fed on-hand. Sample stock is $0,
so there is deliberately NO cost field here.

Separate from the sellable `PurchaseOrder` (different pool) and from the
`SampleInventoryMovement` ledger (audit-only). See
docs/superpowers/specs/2026-06-30-sample-inbound-orders-design.md.
"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SampleInboundOrder(Base):
    __tablename__ = "sample_inbound_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    source: Mapped[str | None] = mapped_column(String(255), nullable=True)  # supplier / origin
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open | received
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lines: Mapped[list["SampleInboundOrderLine"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="SampleInboundOrderLine.id",
    )

    @property
    def unit_count(self) -> int:
        return sum((ln.quantity for ln in self.lines), 0)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def is_received(self) -> bool:
        return self.status == "received"

    @property
    def status_label(self) -> str:
        return {"open": "Open", "received": "Received"}.get(self.status, self.status.title())


class SampleInboundOrderLine(Base):
    __tablename__ = "sample_inbound_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_inbound_order_id: Mapped[int] = mapped_column(
        ForeignKey("sample_inbound_orders.id"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(128), nullable=False)         # display SKU code
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)  # product name snapshot
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    order: Mapped["SampleInboundOrder"] = relationship(back_populates="lines")
