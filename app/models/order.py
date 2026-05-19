"""Orders and order lines.

`order_type` separates paid customer orders from free sample orders so the P&L
engine and sample tracker pull from the same table without double-counting.

Discount split lives on OrderLine (line-level math is the source of truth) and
is rolled up to Order for fast P&L queries.
"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrderType(str, enum.Enum):
    PAID = "paid"
    SAMPLE = "sample"  # free creator/seeding sample (TikTok $0 order)
    PAID_SAMPLE = "paid_sample"  # billed sample — set only when TikTok explicitly flags it


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    tiktok_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.PAID, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    brand: Mapped[str] = mapped_column(String(64), index=True)

    # Roll-ups of line-level values (line is source of truth — see OrderLine).
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    platform_discount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    refunds: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # Fees & marketing back-filled from settlement file.
    # `tiktok_fees` stays as the rolled-up sum of the 8 sub-buckets below
    # so existing queries don't have to change.
    tiktok_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_referral_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_transaction_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_refund_admin_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_sales_tax_on_referral: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_smart_promo_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))   # fee + tax
    tiktok_campaign_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))     # resource + service
    tiktok_partner_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_managed_service: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))   # per-order + tax

    affiliate_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shop_ads_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # Seller-funded discount split (rolled up from OrderLine). The exact-sum
    # invariant holds at every level: outlandish + smashbox == total.
    seller_funded_discount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_outlandish: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_smashbox: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # True if ANY line breached the seller-funded policy cap (default 30% of
    # the line's post-TikTok price). Smashbox still absorbs the excess so the
    # split invariant holds; the flag exposes the violation for investigation.
    discount_policy_violation: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLine(Base):
    """Line-level money. THE SELLER-FUNDED DISCOUNT SPLIT IS COMPUTED HERE.

    The base for the 10% Outlandish cap is `post_tiktok_price`
    (gross_sales − platform_discount), NOT gross_sales.
    """
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)

    sku: Mapped[str] = mapped_column(String(128), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    # Gross / discount stack — all stored as POSITIVE magnitudes.
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    platform_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    post_tiktok_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_outlandish: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_funded_smashbox: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    discount_policy_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    # Finance can acknowledge a flagged line so it stops counting toward the
    # Data Health badge — used when an authorized stack (e.g. customer coupon
    # on top of a seller promo) pushed the line over the policy cap on purpose.
    # The flag itself is preserved for audit; the count is just suppressed.
    policy_violation_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    policy_violation_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # COGS snapshot at import time so historical reports don't shift when the
    # SKU master is edited later.
    unit_cogs_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    order: Mapped[Order] = relationship(back_populates="lines")
