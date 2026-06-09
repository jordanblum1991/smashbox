"""Manually-entered TikTok GMV Max campaign metrics, per (year, month).

These are TikTok's own CAMPAIGN-ATTRIBUTED figures from the GMV Max report in
Seller Center — NOT derivable from our whole-shop order data. TikTok credits a
campaign only for the orders/revenue it drove, so e.g. Feb 2026 attributed just
7 SKU orders even though the shop had 39 paid orders that month. We import the
campaign *spend* (`AdSpend`), but the attributed *revenue* and *SKU orders*
exist only in TikTok's report — so finance types them in here, exactly the way
`TikTokDailyMetric` lets GMV tie out to Seller Center.

Stored fields are the two attributed values TikTok reports:
  - gross_revenue : "amount the user pays + all Shop price subsidies"
  - sku_orders    : attributed SKU orders (Σ distinct SKU per attributed order)
Ad Cost is NOT stored here — it comes from the imported GMV-Max `AdSpend`
(which matched Seller Center's Ad Cost column to the cent). Cost-per-Order and
ROI are derived (see app/reports/gmv_max_campaign_kpis.py).

Edit-not-stack: UNIQUE on (year, month) means re-saving the same month
overwrites in place rather than appending. Mirrors `GmvMaxReimbursement`.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class GmvMaxCampaignMetric(Base):
    __tablename__ = "gmv_max_campaign_metrics"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_gmv_max_campaign_metrics_year_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), index=True, nullable=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    gross_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sku_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
