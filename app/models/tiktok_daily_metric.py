"""Daily metrics imported from TikTok Shop Analytics ("Key metrics" export).

The headline column we care about is `gmv` — that's the value TikTok Seller
Center's "Sales" tile shows for that day. We store the file's full key-metric
set so we can answer follow-up reconciliation questions without re-importing
(tax handling, refund counts, AOV drift, etc.).

The export is uniquely keyed by `metric_date`; re-importing an overlapping
window overwrites prior values for each day (TikTok occasionally revises
yesterday's numbers).
"""
from datetime import date as date_t
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TikTokDailyMetric(Base):
    __tablename__ = "tiktok_daily_metrics"
    __table_args__ = (UniqueConstraint("metric_date", name="uq_tt_daily_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    metric_date: Mapped[date_t] = mapped_column(Date, index=True)

    # Headline "Sales" figure as displayed on the Seller Center dashboard.
    gmv: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0)
    customers: Mapped[int] = mapped_column(Integer, default=0)
    items_sold: Mapped[int] = mapped_column(Integer, default=0)
    items_canceled_returned: Mapped[int] = mapped_column(Integer, default=0)
    items_refunded: Mapped[int] = mapped_column(Integer, default=0)
    aov: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    # GMV including sales tax — useful when the reconciliation gap traces to tax.
    gmv_with_tax: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    shipping_fees: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
