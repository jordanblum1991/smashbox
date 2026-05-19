"""TikTok ad spend — one row per (date, campaign).

Source file: TikTok Ads Manager "Cost" export (`Cost_*.xlsx`). The export has
one row per (date, campaign) plus a footer "Total" row that the importer
skips. TikTok writes ad costs as NEGATIVE numbers; we store the absolute
magnitude so the P&L renderer can subtract directly (matches how
Order.shop_ads_cost works).

The unique key is (spend_date, campaign_id) so re-uploading the same file
or a refreshed file that overlaps prior periods is a no-op upsert.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdSpend(Base):
    __tablename__ = "ad_spend"
    __table_args__ = (
        UniqueConstraint("spend_date", "campaign_id", name="uq_ad_spend_date_campaign"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)

    spend_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)
    campaign_name: Mapped[str | None] = mapped_column(String(512), nullable=True)

    cash_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    credit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    ad_credit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    campaign_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
