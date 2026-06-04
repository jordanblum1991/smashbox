"""Pure helpers for the dashboard's trend affordances: month-over-month deltas
and inline-SVG sparkline coordinates. No DB, no I/O — the route assembles the
data (current + prior MonthlyPnL, trailing series) and feeds these.

Kept separate from monthly_pnl.py because this is presentation-support math
(how a number compares, how a series maps to pixels), not P&L computation.

Delta semantics (MoM vs the previous calendar month):
  - prior missing / prior month had no activity   -> "new"  (no genuine base)
  - prior value is 0 but current isn't            -> "new"  (can't divide; not +inf%)
  - prior and current both 0                       -> "—"    (nothing to compare)
  - otherwise   pct = (current - prior) / abs(prior) * 100, rounded to 0.1%
                abs(prior) keeps direction intuitive for negative bases
                (a loss shrinking from -100 to -50 reads as +50% "up").
A change that rounds to 0.0% is reported as 'flat' so the UI shows no arrow.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_TENTH = Decimal("0.1")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class Delta:
    """A month-over-month change for one metric.

    state : 'up' | 'down' | 'flat' | 'new'
    pct   : signed percentage change (Decimal, 0.1 precision) or None when
            there's no meaningful comparison ('new' / '—')
    label : display string — "+12.4%", "-3.1%", "0.0%", "new", or "—"
    """
    state: str
    pct: Decimal | None
    label: str


# Delta modes — how the change is expressed and what suffix it carries:
#   relative : (cur - prior) / |prior| * 100, "%"  — money/count metrics.
#              Needs prior != 0 (division); zero prior -> "new"/"—".
#   absolute : cur - prior, "x"                     — multipliers (ROAS).
#              Keeps the zero-prior guard: a 0 baseline (no ad spend) isn't a
#              meaningful jump, so it reads "new".
#   points   : (cur - prior) * 100, "pp"            — ratios (margin).
#              Subtraction, so a 0 baseline IS meaningful (0% -> 42% = +42.0pp);
#              no zero-prior guard.
_SUFFIX = {"relative": "%", "absolute": "x", "points": "pp"}


def compute_delta(
    current: Decimal, prior: Decimal | None, *, prior_has_data: bool, mode: str = "relative"
) -> Delta:
    """MoM delta of `current` vs `prior`. See module docstring + _SUFFIX for modes."""
    # No genuine prior to compare against.
    if prior is None or not prior_has_data:
        return Delta(state="new", pct=None, label="new")

    suffix = _SUFFIX[mode]

    if mode == "points":
        raw = (current - prior) * _HUNDRED
    elif mode == "absolute":
        if prior == 0:
            return Delta(state="flat", pct=None, label="—") if current == 0 \
                else Delta(state="new", pct=None, label="new")
        raw = current - prior
    else:  # relative
        if prior == 0:
            return Delta(state="flat", pct=None, label="—") if current == 0 \
                else Delta(state="new", pct=None, label="new")
        raw = (current - prior) / abs(prior) * _HUNDRED

    val = raw.quantize(_TENTH, rounding=ROUND_HALF_UP)
    if val > 0:
        return Delta(state="up", pct=val, label=f"+{val}{suffix}")
    if val < 0:
        return Delta(state="down", pct=val, label=f"{val}{suffix}")
    return Delta(state="flat", pct=val, label=f"0.0{suffix}")


def _fmt(v) -> str:
    return f"{float(v):.2f}"


def sparkline_points(series, width: int = 100, height: int = 32, pad: int = 2) -> str:
    """Map a numeric series to an SVG `points` string for a <polyline>.

    Returns "" for fewer than 2 points (nothing to draw). Y is inverted so the
    largest value sits at the top (smallest y). When every value is equal, the
    line is pinned to the vertical midpoint — no divide-by-zero on min==max.
    """
    vals = list(series)
    if len(vals) < 2:
        return ""

    lo, hi = min(vals), max(vals)
    span = hi - lo
    n = len(vals)
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad

    pts = []
    for i, v in enumerate(vals):
        x = pad + inner_w * i / (n - 1)
        if span == 0:
            y = pad + inner_h / 2
        else:
            frac = (v - lo) / span               # 0 at min, 1 at max
            y = pad + inner_h * (1 - float(frac))  # invert: max -> top
        pts.append(f"{_fmt(x)},{_fmt(y)}")
    return " ".join(pts)


@dataclass(frozen=True)
class Bar:
    """One bar in a zero-baseline bar chart (SVG user units). `sign` is
    'pos' | 'neg' | 'zero' so the renderer can color above/below the line."""
    x: float
    y: float
    w: float
    h: float
    sign: str


@dataclass(frozen=True)
class BarChart:
    bars: list
    baseline: float    # y of the zero line
    width: float
    height: float


def bar_chart(values, *, width: float = 100, height: float = 40, pad: float = 3, gap_ratio: float = 0.3) -> BarChart:
    """Lay out `values` as bars on a shared zero baseline.

    The value range always includes 0, so positive bars rise from the baseline
    and negative bars hang below it; the baseline floats to wherever 0 sits
    (bottom when all-positive, top when all-negative). An all-zero or empty
    series produces zero-height bars with no divide-by-zero. Coordinates are
    floats in a `width` x `height` viewBox; the macro just draws <rect>s.
    """
    vals = list(values)
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad

    if not vals:
        return BarChart(bars=[], baseline=pad + inner_h, width=width, height=height)

    lo = min(min(vals), 0)
    hi = max(max(vals), 0)
    span = hi - lo
    n = len(vals)
    slot = inner_w / n
    bw = slot * (1 - gap_ratio)

    if span == 0:                                  # all zero — flat, no division
        baseline = pad + inner_h
        bars = [Bar(x=pad + i * slot + (slot - bw) / 2, y=baseline, w=bw, h=0.0, sign="zero")
                for i in range(n)]
        return BarChart(bars=bars, baseline=baseline, width=width, height=height)

    def y_of(v) -> float:
        return pad + float(hi - v) / float(span) * inner_h

    baseline = y_of(0)
    bars = []
    for i, v in enumerate(vals):
        x = pad + i * slot + (slot - bw) / 2
        if v > 0:
            top, h, sign = y_of(v), baseline - y_of(v), "pos"
        elif v < 0:
            top, h, sign = baseline, y_of(v) - baseline, "neg"
        else:
            top, h, sign = baseline, 0.0, "zero"
        bars.append(Bar(x=x, y=top, w=bw, h=h, sign=sign))
    return BarChart(bars=bars, baseline=baseline, width=width, height=height)


def trailing_months(year: int, month: int, n: int) -> list[tuple[int, int]]:
    """The `n` most recent (year, month) pairs ending at (year, month),
    oldest first. Walks the calendar backwards so year boundaries are handled
    (e.g. trailing_months(2026, 2, 4) -> ..2025-11, 2025-12, 2026-01, 2026-02)."""
    out: list[tuple[int, int]] = []
    y, m = year, month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


# --------------------------------------------------------------------------- #
# Assembly — turn a trailing run of MonthlyPnL into per-KPI deltas + sparklines
# --------------------------------------------------------------------------- #

# Metric key -> (how to read it off a MonthlyPnL, delta mode). Money/ratio
# metrics return Decimal; orders/units return int (Decimal()-wrapped before
# delta math). The template owns each card's delta *polarity* (higher_better /
# lower_better / neutral) and coloring — only the numeric mode lives here.
_METRICS = {
    "net_profit": (lambda p: p.managed_net_profit, "relative"),
    "gmv": (lambda p: p.gmv, "relative"),
    "net_customer_sales": (lambda p: p.managed_net_customer_sales, "relative"),
    "gross_margin": (lambda p: p.managed_gross_margin, "points"),   # pp, not %
    "net_margin": (lambda p: p.managed_net_margin, "points"),       # pp, not %
    "roas": (lambda p: p.roas, "absolute"),                          # "+0.3x"
    "ad_spend": (lambda p: p.net_ad_spend, "relative"),
    "gross_sales": (lambda p: p.gross_sales, "relative"),
    "gross_profit": (lambda p: p.managed_gross_profit, "relative"),
    "orders": (lambda p: p.orders_count, "relative"),
    "units": (lambda p: p.units_sold, "relative"),
    "aov": (lambda p: p.aov_after_discounts, "relative"),
}


@dataclass(frozen=True)
class MetricTrend:
    """Per-KPI trend bundle for the template: an optional MoM `delta` chip and
    an SVG sparkline `spark` points string (may be "" when too little data)."""
    delta: Delta | None
    spark: str


def build_dashboard_trends(
    db: "Session", ref_year: int, ref_month: int, *, with_delta: bool, trailing: int = 6
) -> dict[str, MetricTrend]:
    """Compute trailing-`trailing`-month sparklines for each headline metric,
    plus a MoM delta vs the previous calendar month when `with_delta` is set.

    `with_delta` is False for aggregate views (YTD/year/range/custom), where the
    headline value is a multi-month total and a single-month MoM delta would be
    apples-to-oranges — the sparkline still renders the recent monthly trend.
    """
    from app.reports.monthly_pnl import compute_monthly_pnl

    series = [compute_monthly_pnl(db, y, m) for (y, m) in trailing_months(ref_year, ref_month, trailing)]
    current = series[-1]
    prior = series[-2] if len(series) >= 2 else None
    # "Genuine" prior = a previous month that actually had paid orders. An empty
    # month (zeros) must read as "new", not a 0 -> N% explosion.
    prior_has_data = prior is not None and prior.orders_count > 0

    out: dict[str, MetricTrend] = {}
    for key, (getter, mode) in _METRICS.items():
        spark = sparkline_points([getter(p) for p in series])
        delta = None
        if with_delta:
            prior_val = Decimal(getter(prior)) if prior is not None else None
            delta = compute_delta(
                Decimal(getter(current)), prior_val, prior_has_data=prior_has_data, mode=mode
            )
        out[key] = MetricTrend(delta=delta, spark=spark)
    return out
