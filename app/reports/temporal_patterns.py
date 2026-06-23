# app/reports/temporal_patterns.py
"""Aggregate time-of-sale patterns for the Timing tab of /reports/sales: PAID-order
revenue by shop-local weekday (avg per occurrence), by hour (24 buckets), by daypart,
and a daily series with a computed trend-shape label + insight callouts. Pure
computation — reads the ORM, returns dataclasses. Revenue is the velocity report's
canonical per-order GMV so the totals reconcile.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderType
from app.services.reporting_tz import placed_local, placed_window

_CENTS = Decimal("0.01")
MIN_TREND_DAYS = 8
TREND_DIR_PCT = 15.0       # 2nd-half vs 1st-half % change for up/down
SPIKY_CV = 0.6             # coefficient of variation above which a series is "spiky"

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAYPARTS = [              # (key, label, hours)
    ("morning", "Morning", range(5, 12)),       # 05:00–11:59
    ("afternoon", "Afternoon", range(12, 17)),  # 12:00–16:59
    ("evening", "Evening", range(17, 22)),       # 17:00–21:59
    ("night", "Night", [22, 23, 0, 1, 2, 3, 4]), # 22:00–04:59
]

# Canonical per-order GMV — identical to app/reports/sales_report.py's bucket revenue.
_REV = (Order.gross_sales + Order.shipping_revenue
        - Order.seller_funded_outlandish - Order.seller_funded_smashbox
        - Order.platform_discount_total - Order.payment_platform_discount)


@dataclass
class DowStat:
    weekday: int
    label: str
    revenue: Decimal
    occurrences: int
    avg_revenue: Decimal
    is_peak: bool


@dataclass
class HourStat:
    hour: int
    label: str
    revenue: Decimal
    is_peak: bool


@dataclass
class DaypartStat:
    key: str
    label: str
    revenue: Decimal
    is_peak: bool


@dataclass
class DayStat:
    day: date
    label: str
    revenue: Decimal


@dataclass
class TrendShape:
    has_enough: bool
    label: str
    detail: str
    direction: str      # up | down | flat | na
    volatility: str     # spiky | steady | na


@dataclass
class TemporalInsights:
    strongest_dow: DowStat | None
    strongest_dow_pct: Decimal | None   # avg_revenue vs the window's daily average
    peak_hour: HourStat | None
    peak_hour_range: str | None         # "12p–1p"
    best_day: DayStat | None
    trend: TrendShape


@dataclass
class TemporalView:
    dow: list[DowStat]
    hours: list[HourStat]
    dayparts: list[DaypartStat]
    daily: list[DayStat]
    top_days: list[DayStat]
    insights: TemporalInsights
    total_revenue: Decimal
    window_start: date
    window_end: date


def _hour_label(h: int) -> str:
    base = h % 12 or 12
    return f"{base}{'a' if h < 12 else 'p'}"


def _hour_range_label(h: int) -> str:
    return f"{_hour_label(h)}–{_hour_label((h + 1) % 24)}"


def _trend_shape(daily: list[DayStat], total_revenue: Decimal, n_days: int) -> TrendShape:
    if n_days < MIN_TREND_DAYS or total_revenue <= 0:
        return TrendShape(has_enough=False, label="Not enough data",
                          detail=f"Need at least {MIN_TREND_DAYS} days of sales to read a trend.",
                          direction="na", volatility="na")
    series = [float(s.revenue) for s in daily]
    mid = n_days // 2
    avg1 = mean(series[:mid]) if series[:mid] else 0.0
    avg2 = mean(series[mid:]) if series[mid:] else 0.0
    pct = ((avg2 - avg1) / avg1 * 100) if avg1 > 0 else (100.0 if avg2 > 0 else 0.0)
    m = mean(series)
    cv = (pstdev(series) / m) if m > 0 else 0.0

    direction = "up" if pct > TREND_DIR_PCT else ("down" if pct < -TREND_DIR_PCT else "flat")
    volatility = "spiky" if cv > SPIKY_CV else "steady"
    label = ("Trending up" if direction == "up"
             else "Trending down" if direction == "down"
             else "Spiky" if volatility == "spiky" else "Steady")
    even = "uneven day-to-day" if volatility == "spiky" else "fairly even day-to-day"
    detail = f"2nd half {'+' if pct >= 0 else ''}{pct:.0f}% vs 1st; {even}."
    return TrendShape(has_enough=True, label=label, detail=detail,
                      direction=direction, volatility=volatility)


def compute_temporal_patterns(db: Session, *, start: date, end: date) -> TemporalView:
    q_start = datetime(start.year, start.month, start.day)
    q_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
    src_start, src_end = placed_window(q_start, q_end)

    rows = db.execute(
        select(Order.placed_at, _REV.label("rev"))
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= src_start)
        .where(Order.placed_at < src_end)
    ).all()

    dow_rev: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    hour_rev: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    day_rev: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    # Every selected row buckets in-window by construction: placed_window is the
    # inverse of placed_local at the boundary, so placed_local(placed).date() always
    # lands in [start, end] — hence no need to guard the day bucket.
    for placed, rev in rows:
        rev = rev or Decimal("0")
        local = placed_local(placed)
        dow_rev[local.weekday()] += rev
        hour_rev[local.hour] += rev
        day_rev[local.date()] += rev

    n_days = (end - start).days + 1
    window_days = [start + timedelta(days=i) for i in range(n_days)]
    occ: dict[int, int] = defaultdict(int)
    for d in window_days:
        occ[d.weekday()] += 1

    total_revenue = sum(dow_rev.values(), Decimal("0")).quantize(_CENTS)

    dow = []
    for wd in range(7):
        rev = dow_rev.get(wd, Decimal("0"))
        occurrences = occ.get(wd, 0)
        avg = (rev / occurrences).quantize(_CENTS) if occurrences else Decimal("0.00")
        dow.append(DowStat(weekday=wd, label=_WEEKDAYS[wd], revenue=rev.quantize(_CENTS),
                           occurrences=occurrences, avg_revenue=avg, is_peak=False))
    peak_dow = max((d for d in dow if d.occurrences), key=lambda d: d.avg_revenue, default=None)
    if peak_dow and peak_dow.avg_revenue > 0:
        peak_dow.is_peak = True
    else:
        peak_dow = None

    hours = [HourStat(hour=h, label=_hour_label(h),
                      revenue=hour_rev.get(h, Decimal("0")).quantize(_CENTS), is_peak=False)
             for h in range(24)]
    peak_hour = max(hours, key=lambda x: x.revenue, default=None)
    if peak_hour and peak_hour.revenue > 0:
        peak_hour.is_peak = True
    else:
        peak_hour = None

    dayparts = []
    for key, label, hrs in _DAYPARTS:
        rev = sum((hour_rev.get(h, Decimal("0")) for h in hrs), Decimal("0")).quantize(_CENTS)
        dayparts.append(DaypartStat(key=key, label=label, revenue=rev, is_peak=False))
    peak_dp = max(dayparts, key=lambda x: x.revenue, default=None)
    if peak_dp and peak_dp.revenue > 0:
        peak_dp.is_peak = True

    daily = [DayStat(day=d, label=f"{d:%b} {d.day}", revenue=day_rev.get(d, Decimal("0")).quantize(_CENTS))
             for d in window_days]
    top_days = sorted((s for s in daily if s.revenue > 0), key=lambda s: s.revenue, reverse=True)[:3]

    trend = _trend_shape(daily, total_revenue, n_days)

    daily_avg = (total_revenue / n_days) if n_days else Decimal("0")
    strongest_dow_pct = None
    if peak_dow and daily_avg > 0:
        strongest_dow_pct = ((peak_dow.avg_revenue - daily_avg) / daily_avg * 100).quantize(Decimal("0.1"))
    insights = TemporalInsights(
        strongest_dow=peak_dow, strongest_dow_pct=strongest_dow_pct,
        peak_hour=peak_hour, peak_hour_range=(_hour_range_label(peak_hour.hour) if peak_hour else None),
        best_day=(top_days[0] if top_days else None), trend=trend,
    )

    return TemporalView(dow=dow, hours=hours, dayparts=dayparts, daily=daily,
                        top_days=top_days, insights=insights, total_revenue=total_revenue,
                        window_start=start, window_end=end)
