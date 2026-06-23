# app/reports/sku_performance.py
"""Per-SKU sales performance for the selected period vs the immediately-prior
equal-length period: units, net sales, orders, momentum, a 6-status lifecycle,
a per-SKU sparkline, and "act on this" insights. PAID orders only. Pure
computation — reads the ORM, returns dataclasses (the SKUs tab of /reports/sales).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.reports.dashboard_trends import Delta, compute_delta, sparkline_points
from app.services.reporting_tz import placed_local_date, placed_window
from app.services.sku_resolver import catalog_label_map

_CENTS = Decimal("0.01")
RISING_PCT = Decimal("25")               # ±25% momentum band
# Lifecycle dead band: |Δ%| <= 25 reads "steady" even though the momentum chip still shows the exact up/down change.
SORTS = ("units", "net_sales", "orders", "momentum")


@dataclass
class SkuPerfRow:
    sku_id: str
    code: str
    name: str
    units: int
    net_sales: Decimal
    orders: int
    pct_units: Decimal
    prior_units: int
    momentum: Delta | None
    status: str                          # new|rising|steady|declining|stalled|inactive
    spark: str


@dataclass
class SkuInsights:
    top_seller: SkuPerfRow | None
    biggest_riser: SkuPerfRow | None
    biggest_faller: SkuPerfRow | None
    new_count: int
    stalled_count: int


@dataclass
class SkuPerformanceView:
    rows: list[SkuPerfRow]
    inactive_rows: list[SkuPerfRow]
    inactive_count: int
    insights: SkuInsights
    total_units: int
    total_net_sales: Decimal
    window_start: date
    window_end: date


_NET = (OrderLine.gross_sales - OrderLine.platform_discount
        - OrderLine.seller_funded_outlandish - OrderLine.seller_funded_smashbox)


def _src_bounds(start: date, end: date):
    """Source-tz [start_inclusive, end_exclusive) for a shop-local [start, end]."""
    q_start = datetime(start.year, start.month, start.day)
    q_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return placed_window(q_start, q_end)


def _paid_lines(db: Session, start: date, end: date):
    src_start, src_end = _src_bounds(start, end)
    return db.execute(
        select(OrderLine.order_id, OrderLine.sku, OrderLine.quantity,
               _NET.label("net"), Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= src_start)
        .where(Order.placed_at < src_end)
    ).all()


def _classify(cur: int, prior: int, is_new: bool) -> str:
    if cur == 0 and prior == 0:
        return "inactive"
    if prior > 0 and cur == 0:
        return "stalled"
    if is_new:
        return "new"
    if cur > 0 and prior == 0:
        return "rising"                  # reactivated after a gap
    pct = (Decimal(cur - prior) / Decimal(prior)) * 100
    if pct > RISING_PCT:
        return "rising"
    if pct < -RISING_PCT:
        return "declining"
    return "steady"


def _sort_value(r: SkuPerfRow, sort: str):
    if sort == "net_sales":
        return r.net_sales
    if sort == "orders":
        return r.orders
    if sort == "momentum":
        return r.momentum.pct if (r.momentum and r.momentum.pct is not None) else Decimal("-1e9")
    return r.units


def compute_sku_performance(db: Session, *, start: date, end: date,
                            sort: str = "units",
                            as_of: date | None = None,  # reserved (future: flag in-progress window); unused today
                            ) -> SkuPerformanceView:
    if sort not in SORTS:
        sort = "units"
    length = (end - start).days + 1
    prior_start = start - timedelta(days=length)
    prior_end = start - timedelta(days=1)

    # Current-window aggregation.
    cur_units: dict[str, int] = defaultdict(int)
    cur_net: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    cur_orders: dict[str, set] = defaultdict(set)
    daily: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for oid, sku, qty, net, placed in _paid_lines(db, start, end):
        cur_units[sku] += qty
        cur_net[sku] += net or Decimal("0")
        cur_orders[sku].add(oid)
        daily[sku][placed_local_date(placed)] += qty

    prior_units: dict[str, int] = defaultdict(int)
    for _oid, sku, qty, _net, _placed in _paid_lines(db, prior_start, prior_end):
        prior_units[sku] += qty

    # SKUs that sold before the selected window start (for "new").
    src_start, _ = _src_bounds(start, end)
    sold_before = set(db.execute(
        select(distinct(OrderLine.sku))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at < src_start)
    ).scalars())

    # Catalog name/code map (canonical TikTok SKU ID → code/name) — single SKUs
    # AND bundles, so a bundle sold through TikTok isn't mislabeled "Unmapped".
    catalog = catalog_label_map(db)

    window_days = [start + timedelta(days=i) for i in range(length)]
    total_units = sum(cur_units.values())

    def _row(sku: str) -> SkuPerfRow:
        cur = cur_units.get(sku, 0)
        prior = prior_units.get(sku, 0)
        code, name = catalog.get(sku, ("Unmapped", f"Unmapped SKU {sku}"))
        is_new = cur > 0 and sku not in sold_before
        momentum = compute_delta(Decimal(cur), Decimal(prior),
                                  prior_has_data=prior > 0, mode="relative")
        pct = (Decimal(cur) / Decimal(total_units) * 100).quantize(Decimal("0.1")) if total_units else Decimal("0")
        spark = sparkline_points([daily[sku].get(d, 0) for d in window_days]) if cur else ""
        return SkuPerfRow(
            sku_id=sku, code=code, name=name, units=cur,
            net_sales=cur_net.get(sku, Decimal("0")).quantize(_CENTS),
            orders=len(cur_orders.get(sku, ())), pct_units=pct, prior_units=prior,
            momentum=momentum, status=_classify(cur, prior, is_new), spark=spark,
        )

    active_keys = set(cur_units) | set(prior_units)
    rows = [_row(s) for s in active_keys]
    rows.sort(key=lambda r: _sort_value(r, sort), reverse=True)

    # Inactive = catalog SKUs that sold in NEITHER window.
    inactive_rows = [
        SkuPerfRow(sku_id=sid, code=code, name=name, units=0, net_sales=Decimal("0.00"),
                   orders=0, pct_units=Decimal("0"), prior_units=0, momentum=None,
                   status="inactive", spark="")
        for sid, (code, name) in sorted(catalog.items(), key=lambda kv: kv[1][0])
        if sid not in active_keys
    ]

    risers = [r for r in rows if r.momentum and r.momentum.pct is not None and r.momentum.pct > 0]
    fallers = [r for r in rows if r.momentum and r.momentum.pct is not None and r.momentum.pct < 0]
    insights = SkuInsights(
        top_seller=max(rows, key=lambda r: r.units, default=None),
        biggest_riser=max(risers, key=lambda r: r.momentum.pct, default=None),
        biggest_faller=min(fallers, key=lambda r: r.momentum.pct, default=None),
        new_count=sum(1 for r in rows if r.status == "new"),
        stalled_count=sum(1 for r in rows if r.status == "stalled"),
    )

    return SkuPerformanceView(
        rows=rows, inactive_rows=inactive_rows, inactive_count=len(inactive_rows),
        insights=insights, total_units=total_units,
        total_net_sales=sum(cur_net.values(), Decimal("0")).quantize(_CENTS),
        window_start=start, window_end=end,
    )
