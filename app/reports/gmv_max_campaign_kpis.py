"""GMV Max campaign KPIs — SKU Orders, Cost per Order, Gross Revenue, ROI.

These are TikTok's CAMPAIGN-ATTRIBUTED figures (Seller Center's GMV Max report),
which we cannot derive from whole-shop orders — see
`app/models/gmv_max_campaign_metric.py`. Gross Revenue and SKU Orders are typed
in by finance (`GmvMaxCampaignMetric`); Ad Cost comes from the imported GMV-Max
`AdSpend` (which matched Seller Center's Ad Cost column to the cent). The two
ratios are derived, defined to match what Seller Center displays:

    Cost per Order = Ad Cost ÷ SKU Orders      (denominator is SKU orders)
    ROI            = Gross Revenue ÷ Ad Cost    (a revenue-to-cost multiple)

A month's metric is included when its month-start falls in the [start, end)
window (campaign metrics are month-grained, so partial-month windows can't be
split). Passing no window aggregates all entered months (the page's default,
no-period view). Ad Cost is summed over AdSpend in the same window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ad_spend import AdSpend
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric

_CENT = Decimal("0.01")


@dataclass
class GmvMaxCampaignKpis:
    gross_revenue: Decimal
    sku_orders: int
    ad_cost: Decimal
    cost_per_order: Decimal
    roi: Decimal
    has_data: bool          # any entered metric row in the window


def compute_gmv_max_campaign_kpis(
    db: Session,
    start: datetime | None = None,
    end: datetime | None = None,
) -> GmvMaxCampaignKpis:
    """Aggregate campaign KPIs for the [start, end) window, or all-time when no
    window is given. SKU Orders / Gross Revenue come from entered metrics; Ad
    Cost from GMV-Max AdSpend. Both ratios guard against divide-by-zero."""
    # Metrics are month-grained and the table is tiny (one row per month), so we
    # filter the month window in Python — portable across SQLite and Postgres,
    # no dialect-specific date arithmetic. A month is in-window iff its
    # first-of-month falls in [start.date(), end.date()).
    s_d: date | None = start.date() if start is not None else None
    e_d: date | None = end.date() if end is not None else None
    rows = db.execute(
        select(
            GmvMaxCampaignMetric.year,
            GmvMaxCampaignMetric.month,
            GmvMaxCampaignMetric.gross_revenue,
            GmvMaxCampaignMetric.sku_orders,
        )
    ).all()
    gross_revenue = Decimal("0")
    sku_orders = 0
    n_rows = 0
    for y, m, grv, sko in rows:
        if s_d is not None and not (s_d <= date(y, m, 1) < e_d):
            continue
        gross_revenue += Decimal(str(grv))
        sku_orders += int(sko)
        n_rows += 1
    gross_revenue = gross_revenue.quantize(_CENT)

    spend_stmt = select(func.coalesce(func.sum(AdSpend.amount), 0))
    if start is not None and end is not None:
        spend_stmt = spend_stmt.where(AdSpend.spend_date >= start, AdSpend.spend_date < end)
    ad_cost = Decimal(str(db.execute(spend_stmt).scalar() or 0)).quantize(_CENT)

    cost_per_order = (ad_cost / sku_orders).quantize(_CENT) if sku_orders else Decimal("0")
    roi = (gross_revenue / ad_cost).quantize(_CENT) if ad_cost else Decimal("0")

    return GmvMaxCampaignKpis(
        gross_revenue=gross_revenue,
        sku_orders=sku_orders,
        ad_cost=ad_cost,
        cost_per_order=cost_per_order,
        roi=roi,
        has_data=n_rows > 0,
    )
