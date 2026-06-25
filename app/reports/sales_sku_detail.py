"""Per-SKU sales drill-down (sub-project C).

A sales-lens detail for one SKU over the selected period: the performance row
(reusing compute_sku_performance for stats/cover/refund), a fixed 12-week trend,
the most recent orders, and bundle membership. Pure computation — the router
renders it; cross-links to the demand-planner drill-down for the buying lens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.demand_planning import BundleRelationship, _bundle_relationships
from app.reports.sku_performance import (
    SkuPerfRow,
    compute_sku_performance,
)
from app.services.reporting_tz import now_local, placed_local_date, placed_window
from app.services.sku_resolver import catalog_label_map

_CENTS = Decimal("0.01")
_NET = (OrderLine.gross_sales - OrderLine.platform_discount
        - OrderLine.seller_funded_outlandish - OrderLine.seller_funded_smashbox)
TREND_WEEKS = 12
RECENT_LIMIT = 20


@dataclass
class WeekPoint:
    week_start: date     # Monday
    units: int
    revenue: Decimal


@dataclass
class RecentOrder:
    placed_at: datetime
    tiktok_order_id: str
    qty: int
    gross: Decimal
    net: Decimal
    refunded: bool       # the order carried a refund (> 0)


@dataclass
class SalesSkuDetail:
    sku_id: str
    code: str
    name: str
    window_start: date
    window_end: date
    row: SkuPerfRow | None
    weekly_trend: list[WeekPoint] = field(default_factory=list)
    recent_orders: list[RecentOrder] = field(default_factory=list)
    bundle_parents: list[BundleRelationship] = field(default_factory=list)
    bundle_components: list[BundleRelationship] = field(default_factory=list)


def _weekly_trend(db: Session, sku_id: str, *, as_of: date) -> list[WeekPoint]:
    """Units + net revenue per ISO week for the trailing TREND_WEEKS, anchored
    on `as_of`'s week (Monday). Sales-lens: order-line `sku` level, not
    bundle-expanded."""
    monday = as_of - timedelta(days=as_of.weekday())
    start = monday - timedelta(weeks=TREND_WEEKS - 1)
    end = monday + timedelta(weeks=1)
    src_start, src_end = placed_window(
        datetime(start.year, start.month, start.day),
        datetime(end.year, end.month, end.day))

    units: dict[date, int] = {}
    revenue: dict[date, Decimal] = {}
    rows = db.execute(
        select(OrderLine.quantity, _NET.label("net"), Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.sku == sku_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= src_start, Order.placed_at < src_end)
    ).all()
    for qty, net, placed in rows:
        d = placed_local_date(placed)
        wk = d - timedelta(days=d.weekday())
        units[wk] = units.get(wk, 0) + int(qty or 0)
        revenue[wk] = revenue.get(wk, Decimal("0")) + (net or Decimal("0"))

    out = []
    for i in range(TREND_WEEKS):
        wk = start + timedelta(weeks=i)
        out.append(WeekPoint(week_start=wk, units=units.get(wk, 0),
                             revenue=revenue.get(wk, Decimal("0")).quantize(_CENTS)))
    return out


def _recent_orders(db: Session, sku_id: str) -> list[RecentOrder]:
    rows = db.execute(
        select(Order.placed_at, Order.tiktok_order_id, OrderLine.quantity,
               OrderLine.gross_sales, _NET.label("net"), Order.refunds)
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.sku == sku_id)
        .where(Order.order_type == OrderType.PAID)
        .order_by(Order.placed_at.desc())
        .limit(RECENT_LIMIT)
    ).all()
    return [
        RecentOrder(
            placed_at=placed, tiktok_order_id=oid, qty=int(qty or 0),
            gross=(gross or Decimal("0")).quantize(_CENTS),
            net=(net or Decimal("0")).quantize(_CENTS),
            refunded=bool(refunds and refunds > 0),
        )
        for placed, oid, qty, gross, net, refunds in rows
    ]


def compute_sales_sku_detail(db: Session, sku_id: str, *, start: date, end: date,
                             as_of: date | None = None) -> SalesSkuDetail:
    as_of = as_of or now_local().date()

    view = compute_sku_performance(db, start=start, end=end)
    row = next((r for r in view.rows if r.sku_id == sku_id), None)

    # Header code/name: from the row, else the catalog, else Unmapped.
    sku_obj = db.execute(
        select(Sku).where((Sku.tiktok_sku_id == sku_id) | (Sku.sku == sku_id)
                          | (Sku.tiktok_alt_sku == sku_id))
    ).scalars().first()
    if row is not None:
        code, name = row.code, row.name
    elif sku_obj is not None:
        code, name = sku_obj.sku, sku_obj.name
    else:
        code, name = catalog_label_map(db).get(sku_id, ("Unmapped", f"Unmapped SKU {sku_id}"))

    parents, components = _bundle_relationships(db, sku_obj, sku_id)

    return SalesSkuDetail(
        sku_id=sku_id, code=code, name=name, window_start=start, window_end=end,
        row=row,
        weekly_trend=_weekly_trend(db, sku_id, as_of=as_of),
        recent_orders=_recent_orders(db, sku_id),
        bundle_parents=parents, bundle_components=components,
    )
