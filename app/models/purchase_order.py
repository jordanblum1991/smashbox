"""Purchase Orders — outbound POs we send to our supplier to replenish stock.

Seeded from the demand planner's reorder recommendations, then editable as a
DRAFT (change quantities, add/remove items) before being PLACED. Placing
finalizes the PO and freezes it; a WeasyPrint PDF is generated to send to the
supplier (auto-email is a later add-on).

Distinct from `purchase_invoices` (the inbound AP ledger of invoices we RECEIVE):
a PO is what we send to order goods; the invoice is what comes back to be paid.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.utcnow()


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # PO-0001
    supplier: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)  # draft | placed
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="PurchaseOrderLine.id",
    )

    @property
    def total(self) -> Decimal:
        return sum((ln.line_total for ln in self.lines), Decimal("0"))

    @property
    def unit_count(self) -> int:
        return sum((ln.quantity for ln in self.lines), 0)

    @property
    def is_placed(self) -> bool:
        return self.status == "placed"

    @property
    def is_received(self) -> bool:
        return self.status == "received"

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"

    @property
    def status_label(self) -> str:
        return {"draft": "Draft", "placed": "Placed", "received": "Received"}.get(
            self.status, self.status.title()
        )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(128), nullable=False)        # display SKU code
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)  # product name snapshot
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"), nullable=False)

    order: Mapped["PurchaseOrder"] = relationship(back_populates="lines")

    @property
    def line_total(self) -> Decimal:
        return Decimal(self.quantity) * self.unit_cost
