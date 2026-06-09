"""GMV Max campaign KPIs — SKU Orders, Cost per Order, Gross Revenue, ROI, and
Ad Cost — aggregated from the imported daily campaign report (`GmvMaxDailyMetric`).

These are TikTok's CAMPAIGN-ATTRIBUTED figures, so the page mirrors TikTok's GMV
Max report to the cent. Because the source is daily, any [start, end) window
(month, all-time, or an arbitrary date range) aggregates exactly. Definitions
match what Seller Center displays:

    Cost per Order = Ad Cost ÷ SKU Orders
    ROI            = Gross Revenue ÷ Ad Cost
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.gmv_max_daily_metric import GmvMaxDailyMetric

_CENT = Decimal("0.01")


@dataclass
class GmvMaxCampaignKpis:
    gross_revenue: Decimal
    sku_orders: int
    ad_cost: Decimal
    cost_per_order: Decimal
    roi: Decimal
    has_data: bool          # any nonzero campaign activity in the window


def compute_gmv_max_campaign_kpis(
    db: Session,
    start: datetime | None = None,
    end: datetime | None = None,
) -> GmvMaxCampaignKpis:
    """Aggregate the daily campaign metrics over [start, end) (exclusive end),
    or all-time when no window is given. Both ratios guard divide-by-zero."""
    stmt = select(
        func.coalesce(func.sum(GmvMaxDailyMetric.gross_revenue), 0),
        func.coalesce(func.sum(GmvMaxDailyMetric.sku_orders), 0),
        func.coalesce(func.sum(GmvMaxDailyMetric.cost), 0),
    )
    if start is not None and end is not None:
        stmt = stmt.where(
            GmvMaxDailyMetric.metric_date >= start.date(),
            GmvMaxDailyMetric.metric_date < end.date(),
        )
    gr_raw, sku_raw, cost_raw = db.execute(stmt).one()

    gross_revenue = Decimal(str(gr_raw)).quantize(_CENT)
    sku_orders = int(sku_raw or 0)
    ad_cost = Decimal(str(cost_raw)).quantize(_CENT)
    cost_per_order = (ad_cost / sku_orders).quantize(_CENT) if sku_orders else Decimal("0")
    roi = (gross_revenue / ad_cost).quantize(_CENT) if ad_cost else Decimal("0")

    return GmvMaxCampaignKpis(
        gross_revenue=gross_revenue,
        sku_orders=sku_orders,
        ad_cost=ad_cost,
        cost_per_order=cost_per_order,
        roi=roi,
        has_data=(sku_orders > 0 or ad_cost > 0 or gross_revenue > 0),
    )
