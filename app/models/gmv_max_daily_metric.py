"""Daily GMV Max campaign metrics, imported from TikTok's "Campaign overview"
export (By-Day view).

This is TikTok's CAMPAIGN-ATTRIBUTED data — the figures it credits to the GMV
Max campaign, which can't be derived from whole-shop orders. The export gives
one row per day with Cost, SKU orders, Cost per order, Gross revenue, and ROI;
we store the three additive values (cost, sku_orders, gross_revenue) and derive
cost-per-order / ROI when aggregating. The Ad Spend page reads this table, so it
mirrors TikTok's GMV Max report to the cent at any date granularity.

Uniquely keyed by `metric_date`; re-importing an overlapping window overwrites
each day (TikTok revises recent days), so the importer is idempotent.
"""
from datetime import date as date_t
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GmvMaxDailyMetric(Base):
    __tablename__ = "gmv_max_daily_metrics"
    __table_args__ = (UniqueConstraint("metric_date", name="uq_gmv_max_daily_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    metric_date: Mapped[date_t] = mapped_column(Date, index=True)

    # Additive figures from the report; cost-per-order and ROI are derived.
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    sku_orders: Mapped[int] = mapped_column(Integer, default=0)
    gross_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
