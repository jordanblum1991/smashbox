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

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = (
        # Natural key for idempotent upsert: one settlement row per (order, statement).
        # An order can legitimately appear in multiple statements over time (e.g.
        # original sale + later refund) but only once per statement.
        UniqueConstraint("tiktok_order_id", "linked_statement_id", name="uq_settlement_order_statement"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

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
    # Rolled-up TikTok fees (sum of the 8 sub-buckets below).
    tiktok_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_referral_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_transaction_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_refund_admin_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_sales_tax_on_referral: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_smart_promo_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_campaign_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_partner_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tiktok_managed_service: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    affiliate_commission: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shop_ads_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))

    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Adjustment(Base):
    """Settlement-level adjustments (logistics reimbursements, bill payments, etc).
    Not tied to a single order — booked at the statement level."""
    __tablename__ = "adjustments"
    __table_args__ = (
        # Natural key for idempotent upsert. adjustment_id alone is NOT unique
        # — TikTok reuses the same ID for paired balance/deduction rows that
        # net to zero. Adding adjustment_type distinguishes the pair.
        UniqueConstraint(
            "adjustment_id", "adjustment_type", "create_time",
            name="uq_adjustment_natural_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    adjustment_id: Mapped[str] = mapped_column(String(64), index=True)
    adjustment_type: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    create_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    settlement_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    linked_statement_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linked_payout_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
