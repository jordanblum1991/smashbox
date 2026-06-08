"""Seller Center KPI group — figures built to match TikTok Seller Center exactly.

GMV / Orders / Items sold come straight from TikTok's own Shop Analytics daily
export (`TikTokDailyMetric`), summed over the period — exact by construction.
AOV is GMV ÷ Orders (TikTok's definition, verified to the cent Mar–May 2026).
SKU orders is computed from order lines (`Σ COUNT(DISTINCT sku)` per PAID order)
because it isn't in the export.

The export is a snapshot, so we surface coverage metadata (`as_of`, `complete`)
to keep a stale in-progress period from being read as truth. Customers is
deliberately omitted — period-unique customers isn't derivable from the daily
export (it doesn't sum) and we have no exact source.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.services.reporting_tz import placed_window


@dataclass
class SellerCenterKpis:
    gmv: Decimal
    orders: int
    items_sold: int
    sku_orders: int
    aov: Decimal
    as_of: date | None        # last analytics day present in the period
    days_covered: int         # number of analytics days in the period
    period_days: int          # calendar days in the selected period
    complete: bool            # export covers through the period's last day

    @property
    def has_data(self) -> bool:
        return self.as_of is not None


def compute_seller_center_kpis(
    db: Session, start: datetime, end: datetime
) -> SellerCenterKpis:
    """Seller-Center-matched KPIs for the [start, end) window.

    TikTok metrics are filtered on `metric_date` (already TikTok's reporting
    day); SKU orders is computed over the shop-local order window so it buckets
    the same way TikTok does.
    """
    start_d, end_d = start.date(), end.date()

    row = db.execute(
        select(
            func.coalesce(func.sum(TikTokDailyMetric.gmv), 0),
            func.coalesce(func.sum(TikTokDailyMetric.orders), 0),
            func.coalesce(func.sum(TikTokDailyMetric.items_sold), 0),
            func.count(TikTokDailyMetric.id),
            func.max(TikTokDailyMetric.metric_date),
        )
        .where(TikTokDailyMetric.metric_date >= start_d)
        .where(TikTokDailyMetric.metric_date < end_d)
    ).one()
    gmv = Decimal(str(row[0])).quantize(Decimal("0.01"))
    orders = int(row[1] or 0)
    items_sold = int(row[2] or 0)
    days_covered = int(row[3] or 0)
    as_of = row[4]

    aov = (gmv / orders).quantize(Decimal("0.01")) if orders else Decimal("0")

    # SKU orders: Σ distinct SKU per PAID order, bucketed shop-local.
    p_start, p_end = placed_window(start, end)
    line_rows = db.execute(
        select(OrderLine.order_id, OrderLine.sku)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
    ).all()
    per_order: dict[int, set] = {}
    for oid, sku in line_rows:
        per_order.setdefault(oid, set()).add(sku)
    sku_orders = sum(len(s) for s in per_order.values())

    period_days = (end_d - start_d).days
    complete = as_of is not None and as_of >= end_d - timedelta(days=1)

    return SellerCenterKpis(
        gmv=gmv,
        orders=orders,
        items_sold=items_sold,
        sku_orders=sku_orders,
        aov=aov,
        as_of=as_of,
        days_covered=days_covered,
        period_days=period_days,
        complete=complete,
    )
