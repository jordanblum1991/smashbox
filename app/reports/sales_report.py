"""Sales reporting — revenue / units / orders velocity at daily, weekly, or
monthly granularity, with period-over-period trend.

"Sales" = PAID orders only (samples excluded), same as the P&L. Revenue is the
canonical Seller-Center GMV (gross + shipping − seller/platform/payment
discounts), matched to monthly_pnl.MonthlyPnL.gmv so this page ties to the
dashboard. Orders are bucketed by their SHOP-LOCAL placed date (the Seller
Center day), then rolled up to the requested granularity. The bucket containing
today is flagged in-progress and excluded from the trend delta so a partial
period doesn't read as a drop.

Pure computation — reads the ORM, returns dataclasses.
"""
from __future__ import annotations

import calendar as _calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.reports.dashboard_trends import Delta, compute_delta
from app.reports.fiscal_calendar import fiscal_months_for, fiscal_window
from app.services.reporting_tz import now_local, placed_local_date, placed_window, today_local

GRANULARITIES = ("daily", "weekly", "monthly")
FISCAL_MODES = ("fiscal_month", "fiscal_ytd", "fiscal_year")
_CENTS = Decimal("0.01")


@dataclass
class SalesBucket:
    key: str            # stable id (iso date / iso monday / "YYYY-MM")
    label: str          # display label
    start: date
    revenue: Decimal    # canonical GMV
    units: int
    orders: int
    in_progress: bool = False  # the bucket containing today

    @property
    def aov(self) -> Decimal:
        return (self.revenue / self.orders).quantize(_CENTS) if self.orders else Decimal("0")


@dataclass
class SalesReportView:
    granularity: str
    buckets: list[SalesBucket]       # chronological, includes zero-sale buckets
    total_revenue: Decimal
    total_units: int
    total_orders: int
    avg_aov: Decimal
    window_start: date
    window_end: date
    days_in_window: int
    avg_daily_revenue: Decimal       # velocity over the whole window
    avg_daily_units: Decimal
    revenue_delta: Delta | None      # last complete period vs the prior one
    units_delta: Delta | None
    peak: SalesBucket | None         # highest-revenue bucket
    as_of: datetime


def _month_floor(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def _window_for(granularity: str, today: date) -> tuple[date, date]:
    """Inclusive shop-local [start, end] default span per granularity."""
    if granularity == "monthly":
        return _add_months(_month_floor(today), -11), today      # 12 calendar months
    if granularity == "weekly":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(weeks=11), today               # 12 ISO weeks
    return today - timedelta(days=29), today                     # 30 days


def _bucket_start(granularity: str, d: date) -> date:
    if granularity == "monthly":
        return _month_floor(d)
    if granularity == "weekly":
        return d - timedelta(days=d.weekday())                   # Monday
    return d


def _label(granularity: str, start: date) -> str:
    if granularity == "monthly":
        return start.strftime("%b %Y")
    if granularity == "weekly":
        return f"Wk {start.strftime('%b')} {start.day}"
    return f"{start.strftime('%b')} {start.day}"


def _key(granularity: str, start: date) -> str:
    return f"{start.year}-{start.month:02d}" if granularity == "monthly" else start.isoformat()


def _span_starts(granularity: str, win_start: date, win_end: date) -> list[date]:
    """Ordered bucket-start dates covering [win_start, win_end]."""
    out: list[date] = []
    cur = _bucket_start(granularity, win_start)
    end = _bucket_start(granularity, win_end)
    while cur <= end:
        out.append(cur)
        if granularity == "monthly":
            cur = _add_months(cur, 1)
        elif granularity == "weekly":
            cur = cur + timedelta(weeks=1)
        else:
            cur = cur + timedelta(days=1)
    return out


@dataclass
class _BucketDef:
    key: str
    label: str
    start: date


def _calendar_plan(granularity: str, win_start: date, win_end: date) -> tuple[list["_BucketDef"], Callable[[date], str]]:
    """Bucket defs + a date→key mapper for daily/weekly/monthly over the window."""
    defs = [_BucketDef(_key(granularity, s), _label(granularity, s), s)
            for s in _span_starts(granularity, win_start, win_end)]

    def key_of(d: date) -> str:
        return _key(granularity, _bucket_start(granularity, d))

    return defs, key_of


def current_fiscal_ym(today: date) -> tuple[int, int]:
    """(fiscal_year, fiscal_month) containing `today`. A fiscal month closes on
    the 28th, so days 1–28 belong to that calendar month's fiscal period and
    days 29–31 roll into the next fiscal month (which may cross the year)."""
    if today.day <= 28:
        return today.year, today.month
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def _fiscal_month_plan(fiscal_year: int, months: list[int]) -> tuple[list["_BucketDef"], Callable[[date], "str | None"]]:
    """One bucket per fiscal month (29th–28th), labeled by closing month, plus a
    date→key mapper that finds the fiscal month whose window contains a date."""
    defs: list[_BucketDef] = []
    windows: list[tuple[str, date, date]] = []
    for mm in months:
        s, e = fiscal_window(fiscal_year, mm)
        k = f"F{fiscal_year}-{mm:02d}"
        defs.append(_BucketDef(k, f"{_calendar.month_abbr[mm]} {fiscal_year}", s))
        windows.append((k, s, e))

    def key_of(d: date):
        for k, s, e in windows:
            if s <= d <= e:
                return k
        return None

    return defs, key_of


def _summarize(db: Session, defs: list[_BucketDef], key_of,
               win_start: date, win_end: date, granularity_value: str,
               today: date) -> SalesReportView:
    """Seed the given buckets, sum PAID orders in [win_start, win_end] into them
    via key_of(placed_local_date), and compute totals / deltas / peak. The single
    aggregation core shared by every scope so they can't drift."""
    if win_start > win_end:
        raise ValueError(f"win_start {win_start} is after win_end {win_end}")
    today_key = key_of(today)
    buckets: dict[str, SalesBucket] = {}
    order_keys: list[str] = []
    for d in defs:
        buckets[d.key] = SalesBucket(key=d.key, label=d.label, start=d.start,
                                     revenue=Decimal("0"), units=0, orders=0,
                                     in_progress=(d.key == today_key))
        order_keys.append(d.key)

    q_start = datetime(win_start.year, win_start.month, win_start.day)
    q_end = datetime(win_end.year, win_end.month, win_end.day) + timedelta(days=1)
    src_start, src_end = placed_window(q_start, q_end)
    paid_in_window = (
        (Order.order_type == OrderType.PAID)
        & (Order.placed_at >= src_start)
        & (Order.placed_at < src_end)
    )

    units_by_order = dict(db.execute(
        select(OrderLine.order_id, func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(paid_in_window)
        .group_by(OrderLine.order_id)
    ).all())

    rows = db.execute(
        select(
            Order.id, Order.placed_at, Order.gross_sales, Order.shipping_revenue,
            Order.seller_funded_outlandish, Order.seller_funded_smashbox,
            Order.platform_discount_total, Order.payment_platform_discount,
        ).where(paid_in_window)
    ).all()

    for r in rows:
        b = buckets.get(key_of(placed_local_date(r.placed_at)))
        if b is None:
            continue
        b.revenue += (r.gross_sales + r.shipping_revenue
                      - r.seller_funded_outlandish - r.seller_funded_smashbox
                      - r.platform_discount_total - r.payment_platform_discount)
        b.units += int(units_by_order.get(r.id, 0))
        b.orders += 1

    ordered = [buckets[k] for k in order_keys]
    for b in ordered:
        b.revenue = b.revenue.quantize(_CENTS)

    total_revenue = sum((b.revenue for b in ordered), Decimal("0"))
    total_units = sum(b.units for b in ordered)
    total_orders = sum(b.orders for b in ordered)
    avg_aov = (total_revenue / total_orders).quantize(_CENTS) if total_orders else Decimal("0")
    days = (win_end - win_start).days + 1
    avg_daily_revenue = (total_revenue / days).quantize(_CENTS) if days else Decimal("0")
    avg_daily_units = round(total_units / days, 1) if days else 0.0

    complete = [b for b in ordered if not b.in_progress]
    revenue_delta = units_delta = None
    if len(complete) >= 2:
        cur, prior = complete[-1], complete[-2]
        has = prior.orders > 0
        revenue_delta = compute_delta(cur.revenue, prior.revenue, prior_has_data=has, mode="relative")
        units_delta = compute_delta(Decimal(cur.units), Decimal(prior.units), prior_has_data=has, mode="relative")

    peak = max(ordered, key=lambda b: b.revenue, default=None)
    if peak is not None and peak.revenue == 0:
        peak = None

    return SalesReportView(
        granularity=granularity_value, buckets=ordered,
        total_revenue=total_revenue, total_units=total_units, total_orders=total_orders,
        avg_aov=avg_aov, window_start=win_start, window_end=win_end, days_in_window=days,
        avg_daily_revenue=avg_daily_revenue, avg_daily_units=avg_daily_units,
        revenue_delta=revenue_delta, units_delta=units_delta, peak=peak,
        as_of=now_local(),
    )


def compute_sales_report(db: Session, granularity: str = "daily", *,
                         start: date | None = None, end: date | None = None,
                         fiscal_year: int | None = None, fiscal_month: int | None = None,
                         as_of: date | None = None) -> SalesReportView:
    today = as_of or today_local()

    if granularity in FISCAL_MODES:
        cur_y, cur_m = current_fiscal_ym(today)
        fy = fiscal_year or cur_y
        fm = fiscal_month or cur_m
        if granularity == "fiscal_month":
            win_start, win_end = fiscal_window(fy, fm)
            defs, key_of = _calendar_plan("daily", win_start, win_end)
        else:
            mode = "ytd" if granularity == "fiscal_ytd" else "year"
            months = fiscal_months_for(mode, fm)
            win_start, _ = fiscal_window(fy, months[0])
            _, win_end = fiscal_window(fy, months[-1])
            defs, key_of = _fiscal_month_plan(fy, months)
        return _summarize(db, defs, key_of, win_start, win_end, granularity, today)

    if granularity not in GRANULARITIES:
        granularity = "daily"
    if (start is None) != (end is None):
        raise ValueError("start and end must both be set or both be omitted")
    if start is not None and end is not None:
        win_start, win_end = start, end
    else:
        win_start, win_end = _window_for(granularity, today)
    defs, key_of = _calendar_plan(granularity, win_start, win_end)
    return _summarize(db, defs, key_of, win_start, win_end, granularity, today)
