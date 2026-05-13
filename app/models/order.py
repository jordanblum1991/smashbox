"""Orders and order lines.

`order_type` separates paid customer orders from free sample orders so the P&L
engine and sample tracker pull from the same table without double-counting.
"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrderType(str, enum.Enum):
    PAID = "paid"
    SAMPLE = "sample"  # free creator/seeding sample (allowance)
    PAID_SAMPLE = "paid_sample"  # oversampling — billed to us


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    tiktok_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.PAID, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # e.g. completed, refunded
    brand: Mapped[str] = mapped_column(String(64), index=True)

    # Order-level money fields (line totals plus order-level adjustments).
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    refunds: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # Fees & marketing pulled in from settlement files when available.
    tiktok_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    affiliate_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shop_ads_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # Seller-funded discount split — Outlandish + Smashbox MUST equal total.
    # See app/rules/seller_funded_split.py.
    seller_funded_discount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_outlandish: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_smashbox: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # True when the TOTAL seller-funded discount exceeded
    # settings.seller_funded_policy_cap_pct of gross_sales — should never happen
    # under our policy. Smashbox still absorbs the excess to keep the split
    # invariant intact; the flag exposes the violation for investigation.
    discount_policy_violation: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)

    sku: Mapped[str] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    # COGS snapshot at import time so historical reports don't shift when the SKU
    # master is edited.
    unit_cogs_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    order: Mapped[Order] = relationship(back_populates="lines")
