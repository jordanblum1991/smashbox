# app/reports/sku_performance.py
"""Per-SKU sales performance for the selected period vs the immediately-prior
equal-length period: units, net sales, orders, momentum, a 6-status lifecycle,
a per-SKU sparkline, and "act on this" insights. PAID orders only. Pure
computation — reads the ORM, returns dataclasses (the SKUs tab of /reports/sales).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

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
class SkuStats:
    """Granular rate / cadence / forecast metrics for one SKU over the selected
    window. All derived from the per-day units series + window length, so the
    totals reconcile exactly with SkuPerfRow."""
    window_days: int
    days_with_sales: int
    pct_days_active: Decimal
    avg_units_per_day: Decimal                  # calendar basis (headline)
    avg_units_per_selling_day: Decimal | None   # None when nothing sold
    avg_revenue_per_day: Decimal
    avg_units_per_order: Decimal | None         # None when orders == 0
    run_rate_30d: int                           # avg_units_per_day × 30, whole units
    best_day_units: int
    best_day_date: date | None
    volatility_cov: Decimal | None              # std/mean of zero-filled daily units


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
    stats: SkuStats | None = None
    on_hand: int | None = None           # latest sellable on-hand (None = no snapshot/bundle/unmapped)
    days_of_cover: Decimal | None = None  # on_hand ÷ period avg units/day
    refunded_amount: Decimal = Decimal("0")   # order refunds attributed by gross share
    refund_rate: Decimal | None = None        # refunded_amount ÷ gross × 100 (None when gross 0)


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
               _NET.label("net"), Order.placed_at,
               OrderLine.gross_sales.label("line_gross"),
               Order.gross_sales.label("order_gross"),
               Order.refunds.label("order_refunds"))
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


def _compute_stats(window_dates: list[date], daily_by_date: dict[date, int], *,
                   units: int, net: Decimal, orders: int) -> SkuStats:
    """Derive the granular stats from the per-day units map over `window_dates`
    (the zero-filled calendar series). `units`/`net`/`orders` are the window
    totals (they reconcile with the daily series)."""
    series = [daily_by_date.get(d, 0) for d in window_dates]
    window_days = len(series)
    days_with_sales = sum(1 for u in series if u > 0)

    def _q(v: Decimal, places="0.01") -> Decimal:
        return v.quantize(Decimal(places))

    avg_per_day = _q(Decimal(units) / Decimal(window_days)) if window_days else Decimal("0")
    avg_per_selling = (_q(Decimal(units) / Decimal(days_with_sales))
                       if days_with_sales else None)
    pct_active = (_q(Decimal(days_with_sales) / Decimal(window_days) * 100, "0.1")
                  if window_days else Decimal("0"))
    avg_rev_per_day = _q(net / Decimal(window_days)) if window_days else Decimal("0")
    avg_per_order = _q(Decimal(units) / Decimal(orders)) if orders else None
    run_rate = int((avg_per_day * 30).to_integral_value(rounding=ROUND_HALF_UP))

    best_units = max(series, default=0)
    best_date = window_dates[series.index(best_units)] if best_units > 0 else None

    # Volatility: coefficient of variation (population std / mean) of the
    # zero-filled series. Undefined for a <2-day window or all-zero series.
    cov: Decimal | None = None
    if window_days >= 2:
        mean = float(units) / window_days
        if mean > 0:
            var = sum((u - mean) ** 2 for u in series) / window_days
            cov = _q(Decimal(str(math.sqrt(var) / mean)))

    return SkuStats(
        window_days=window_days, days_with_sales=days_with_sales,
        pct_days_active=pct_active, avg_units_per_day=avg_per_day,
        avg_units_per_selling_day=avg_per_selling,
        avg_revenue_per_day=avg_rev_per_day, avg_units_per_order=avg_per_order,
        run_rate_30d=run_rate, best_day_units=best_units,
        best_day_date=best_date, volatility_cov=cov,
    )


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
    cur_gross: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    cur_refund: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    daily: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for oid, sku, qty, net, placed, line_gross, order_gross, order_refunds in \
            _paid_lines(db, start, end):
        cur_units[sku] += qty
        cur_net[sku] += net or Decimal("0")
        cur_orders[sku].add(oid)
        daily[sku][placed_local_date(placed)] += qty
        lg = line_gross or Decimal("0")
        cur_gross[sku] += lg
        # Attribute the order's refund $ to this line by gross share.
        if order_refunds and order_gross and order_gross > 0:
            cur_refund[sku] += Decimal(order_refunds) * lg / Decimal(order_gross)

    prior_units: dict[str, int] = defaultdict(int)
    for _oid, sku, qty, *_ in _paid_lines(db, prior_start, prior_end):
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

    # Latest sellable on-hand keyed by physical Sku.sku (= the row's `code`), for
    # days-of-cover. Reuses the demand-planner fold so variations collapse.
    from app.reports.demand_planning import (
        _fold_on_hand_to_physical, _latest_on_hand_per_sku, _physical_key_resolver,
    )
    from app.services.sku_alias import load_alias_map
    _alias_map = load_alias_map(db)
    _oh_raw, _ = _latest_on_hand_per_sku(db, alias_map=_alias_map)
    on_hand_by_physical = _fold_on_hand_to_physical(
        _oh_raw, _physical_key_resolver(db, _alias_map))

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
        net = cur_net.get(sku, Decimal("0")).quantize(_CENTS)
        n_orders = len(cur_orders.get(sku, ()))
        stats = _compute_stats(window_days, daily[sku],
                               units=cur, net=net, orders=n_orders)
        # Days of cover: current sellable on-hand ÷ the period's avg units/day.
        on_hand = on_hand_by_physical.get(code)
        cover = None
        if on_hand is not None and stats.avg_units_per_day > 0:
            cover = (Decimal(on_hand) / stats.avg_units_per_day).quantize(Decimal("0.1"))
        # Refund rate: order refunds attributed to this SKU by gross share.
        gross = cur_gross.get(sku, Decimal("0")).quantize(_CENTS)
        refunded = cur_refund.get(sku, Decimal("0")).quantize(_CENTS)
        refund_rate = ((refunded / gross * 100).quantize(Decimal("0.1"))
                       if gross > 0 else None)
        return SkuPerfRow(
            sku_id=sku, code=code, name=name, units=cur,
            net_sales=net,
            orders=n_orders, pct_units=pct, prior_units=prior,
            momentum=momentum, status=_classify(cur, prior, is_new), spark=spark,
            stats=stats, on_hand=on_hand, days_of_cover=cover,
            refunded_amount=refunded, refund_rate=refund_rate,
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


# CSV export — mirrors the on-screen SKU table (active rows, current sort) plus
# the granular per-SKU stats from the expand panel.
SKU_CSV_HEADER = [
    "SKU", "Name", "TikTok SKU ID", "Units", "Net Sales", "Orders",
    "% of Units", "Prior Units", "Momentum", "Status",
    "Avg Units/Day", "Avg Units/Day (Selling)", "Days Active", "% Days Active",
    "Avg Revenue/Day", "Avg Units/Order", "Run-Rate (30d)",
    "Best Day Units", "Best Day Date", "Volatility (CoV)",
    "On Hand", "Days of Cover", "Refunded $", "Refund %",
]


def sku_performance_csv_rows(view: SkuPerformanceView):
    """Yield one CSV row per active SKU, in the view's current sort order.
    Inactive (zero-sales) SKUs are intentionally excluded — the download
    matches the main on-screen table. Undefined stats render as blank cells."""
    for r in view.rows:
        s = r.stats
        yield [
            r.code, r.name, r.sku_id, r.units, f"{r.net_sales:.2f}", r.orders,
            f"{r.pct_units}", r.prior_units,
            r.momentum.label if r.momentum else "—", r.status,
            s.avg_units_per_day if s else "",
            s.avg_units_per_selling_day if (s and s.avg_units_per_selling_day is not None) else "",
            s.days_with_sales if s else "",
            s.pct_days_active if s else "",
            s.avg_revenue_per_day if s else "",
            s.avg_units_per_order if (s and s.avg_units_per_order is not None) else "",
            s.run_rate_30d if s else "",
            s.best_day_units if s else "",
            s.best_day_date.isoformat() if (s and s.best_day_date) else "",
            s.volatility_cov if (s and s.volatility_cov is not None) else "",
            r.on_hand if r.on_hand is not None else "",
            r.days_of_cover if r.days_of_cover is not None else "",
            f"{r.refunded_amount:.2f}",
            r.refund_rate if r.refund_rate is not None else "",
        ]


# "Needs attention" digest — flag SKUs worth acting on for the scheduled email.
@dataclass
class AttentionDigest:
    decelerating: list[SkuPerfRow]   # momentum down past the band (status=declining)
    spiking: list[SkuPerfRow]        # momentum up sharply (> spike_pct)
    stalled: list[SkuPerfRow]        # sold before, 0 this period
    low_cover: list[SkuPerfRow]      # days-of-cover below the reorder threshold
    counts: dict[str, int]           # full per-category counts (before cap), for "+N more"

    @property
    def any(self) -> bool:
        return any((self.decelerating, self.spiking, self.stalled, self.low_cover))


def build_attention_digest(rows: list[SkuPerfRow], *, low_cover_days: int = 14,
                           spike_pct: int = 50, cap: int = 5) -> AttentionDigest:
    """Bucket SKUs into the four attention categories, each sorted most-urgent
    first and capped at `cap` (full counts retained for a '+N more' note)."""
    decel = sorted((r for r in rows if r.status == "declining"),
                   key=lambda r: r.units, reverse=True)
    spike = sorted((r for r in rows if r.momentum and r.momentum.pct is not None
                    and r.momentum.pct > spike_pct),
                   key=lambda r: r.units, reverse=True)
    stalled = sorted((r for r in rows if r.status == "stalled"),
                     key=lambda r: r.units, reverse=True)
    low = sorted((r for r in rows if r.days_of_cover is not None
                  and r.days_of_cover < low_cover_days),
                 key=lambda r: r.days_of_cover)
    counts = {"decelerating": len(decel), "spiking": len(spike),
              "stalled": len(stalled), "low_cover": len(low)}
    return AttentionDigest(decelerating=decel[:cap], spiking=spike[:cap],
                           stalled=stalled[:cap], low_cover=low[:cap], counts=counts)
