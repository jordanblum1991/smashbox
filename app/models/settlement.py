"""Settlement rows — one per (statement, order).

Sourced from TikTok's `merchant_statement_profit_loss_*.xlsx`. This is the
authoritative P&L breakdown for an order: TikTok's referral & transaction fees,
affiliate / shop-ads commissions, shipping costs, etc. The settlement importer
both creates rows here AND back-fills the matching Order row.

The raw_payload column keeps every TikTok column verbatim so future drill-downs
or schema additions don't require re-importing.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    tiktok_order_id: Mapped[str] = mapped_column(String(64), index=True)
    linked_statement_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linked_payout_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    paid_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    settled_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    order_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_order_type: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # TikTok-derived totals for this order (signed as TikTok reports them — costs negative).
    order_income: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    order_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    net_order_margin: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    # Buckets that feed Order.* fields (POSITIVE magnitudes — see importer for mapping).
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    gross_sales_refund: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    seller_discount_refund: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    affiliate_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shop_ads_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Adjustment(Base):
    """Settlement-level adjustments (logistics reimbursements, bill payments, etc).
    Not tied to a single order — booked at the statement level."""
    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)

    # Not unique: TikTok reuses the same Adjustment ID for paired
    # balance/deduction rows that net to zero.
    adjustment_id: Mapped[str] = mapped_column(String(64), index=True)
    adjustment_type: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    create_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    settlement_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    linked_statement_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linked_payout_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
