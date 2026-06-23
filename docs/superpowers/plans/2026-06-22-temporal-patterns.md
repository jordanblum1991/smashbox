# Temporal Patterns (Timing Tab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill in the **Timing** tab on `/reports/sales` with aggregate temporal patterns — revenue by day-of-week (avg per occurrence), by hour (24 bars), a daily trend with a computed shape label, and insight callouts.

**Architecture:** A new pure-computation module `app/reports/temporal_patterns.py`, a `tab == "timing"` branch in `sales_view`, and a Timing block in `sales.html` reusing the existing `ui.barchart` helper. Spec: `docs/superpowers/specs/2026-06-22-temporal-patterns-design.md`.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Jinja2 + compiled Tailwind, pytest. Branch: `feature/temporal-patterns`.

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25`.
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — `%` breaks it), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Money is `Decimal`. Starlette: `templates.TemplateResponse(request, "x.html", {...})`.

---

## Task 1: `compute_temporal_patterns` module

**Files:**
- Create: `app/reports/temporal_patterns.py`
- Test: `tests/test_temporal_patterns.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_temporal_patterns.py`:

```python
# tests/test_temporal_patterns.py
"""Aggregate temporal patterns: revenue by local weekday/hour/date, day-of-week
avg-per-occurrence, trend-shape classification, insights. Buckets are derived
through placed_local() (the same shop-local conversion the report uses), so the
tests assert against placed_local-derived expectations rather than hardcoding the
DST offset."""
import itertools
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType
from app.reports.temporal_patterns import compute_temporal_patterns
from app.services.reporting_tz import placed_local

_OID = itertools.count(1)
WSTART, WEND = date(2026, 5, 1), date(2026, 5, 31)   # 31-day window


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _order(db, dt, rev, order_type=OrderType.PAID):
    """A PAID order at placed_at=dt whose canonical GMV equals `rev` (all the
    other revenue components are zeroed so the SQL sum is non-NULL)."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
                 order_type=order_type, status="Completed", brand="smashbox",
                 gross_sales=Decimal(str(rev)), shipping_revenue=Decimal("0"),
                 seller_funded_outlandish=Decimal("0"), seller_funded_smashbox=Decimal("0"),
                 platform_discount_total=Decimal("0"), payment_platform_discount=Decimal("0")))
    db.flush()


def test_revenue_buckets_to_local_weekday_and_hour():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _order(db, dt, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    exp_wd, exp_h = placed_local(dt).weekday(), placed_local(dt).hour
    assert v.dow[exp_wd].revenue == Decimal("100.00")
    assert v.hours[exp_h].revenue == Decimal("100.00")
    assert v.hours[exp_h].is_peak
    assert v.total_revenue == Decimal("100.00")
    assert sum((d.revenue for d in v.dow), Decimal("0")) == Decimal("100.00")


def test_avg_per_occurrence():
    dt1, dt2 = datetime(2026, 5, 6, 12, 0), datetime(2026, 5, 13, 12, 0)  # same weekday, +7d
    with SessionLocal() as db:
        _order(db, dt1, 100); _order(db, dt2, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    wd = placed_local(dt1).weekday()
    assert placed_local(dt2).weekday() == wd
    stat = v.dow[wd]
    assert stat.revenue == Decimal("200.00")
    assert stat.occurrences >= 2
    assert stat.avg_revenue == (Decimal("200.00") / stat.occurrences).quantize(Decimal("0.01"))
    assert stat.is_peak


def test_peak_weekday_and_insights():
    sat, mon = datetime(2026, 5, 9, 12, 0), datetime(2026, 5, 11, 12, 0)
    with SessionLocal() as db:
        _order(db, sat, 500); _order(db, mon, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    wd_sat = placed_local(sat).weekday()
    assert v.dow[wd_sat].is_peak
    assert v.insights.strongest_dow.weekday == wd_sat
    assert v.insights.peak_hour is not None
    assert v.insights.peak_hour_range is not None
    assert v.insights.best_day is not None
    assert v.insights.best_day.revenue == Decimal("500.00")


def test_trend_up():
    base = date(2026, 5, 1)
    with SessionLocal() as db:
        for i in range(16):
            d = base + timedelta(days=i)
            _order(db, datetime(d.year, d.month, d.day, 12, 0), 10 + i * 10)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=15))
    assert v.insights.trend.has_enough
    assert v.insights.trend.label == "Trending up"
    assert v.insights.trend.direction == "up"


def test_trend_steady():
    base = date(2026, 5, 1)
    with SessionLocal() as db:
        for i in range(14):
            d = base + timedelta(days=i)
            _order(db, datetime(d.year, d.month, d.day, 12, 0), 100)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=13))
    assert v.insights.trend.label == "Steady"
    assert v.insights.trend.direction == "flat"
    assert v.insights.trend.volatility == "steady"


def test_trend_spiky():
    base = date(2026, 5, 1)
    vals = [0, 0, 500, 0, 0, 0, 0, 0, 0, 500, 0, 0, 0, 0]  # a burst in each half → flat dir, high CV
    with SessionLocal() as db:
        for i, val in enumerate(vals):
            if val:
                d = base + timedelta(days=i)
                _order(db, datetime(d.year, d.month, d.day, 12, 0), val)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=len(vals) - 1))
    assert v.insights.trend.volatility == "spiky"
    assert v.insights.trend.label == "Spiky"


def test_dayparts_sum_hours():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _order(db, dt, 250); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    h = placed_local(dt).hour
    key = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
           else "evening" if 17 <= h < 22 else "night")
    dp = {d.key: d for d in v.dayparts}[key]
    assert dp.revenue == Decimal("250.00")
    assert dp.is_peak


def test_paid_only_and_empty():
    with SessionLocal() as db:
        _order(db, datetime(2026, 5, 20, 12, 0), 999, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    assert v.total_revenue == Decimal("0.00")
    assert v.insights.strongest_dow is None
    assert v.insights.peak_hour is None
    assert v.insights.best_day is None
    assert not v.insights.trend.has_enough
    assert v.top_days == []
```

- [ ] **Step 2: Run to verify it fails** — `py -m pytest tests/test_temporal_patterns.py -v 2>&1 | tail -20`. Expected: `No module named 'app.reports.temporal_patterns'`.

- [ ] **Step 3: Create the module** — `app/reports/temporal_patterns.py`:

```python
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
    peak_hour_range: str | None         # "12–1pm"
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
```

- [ ] **Step 4: Run the tests** — `py -m pytest tests/test_temporal_patterns.py -v 2>&1 | tail -20`. Expected: 8 passed.

IMPORTANT before trusting green: confirm the `Order` revenue field names exist by reading `app/reports/sales_report.py` (around its `select(Order.id, Order.placed_at, Order.gross_sales, Order.shipping_revenue, Order.seller_funded_outlandish, Order.seller_funded_smashbox, Order.platform_discount_total, Order.payment_platform_discount, ...)`) — `_REV` must use exactly those names. If any differ, STOP and report BLOCKED with the actual names.

- [ ] **Step 5: Commit** — `.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
timing: temporal-patterns compute module

compute_temporal_patterns aggregates PAID per-order GMV by shop-local weekday
(avg per occurrence), hour (24 buckets), daypart, and date; classifies the daily
trend shape (Steady/Spiky/Trending up/down, >=8 days) and builds insight
callouts (strongest weekday, peak hour, best day). Pure computation.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/temporal_patterns.py tests/test_temporal_patterns.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`

---

## Task 2: Route + template (enable + render the Timing tab)

**Files:**
- Modify: `app/routers/reports.py` (`sales_view`)
- Modify: `app/templates/reports/sales.html`
- Test: `tests/test_sales_timing_tab.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_sales_timing_tab.py`:

```python
# tests/test_sales_timing_tab.py
"""The Timing tab on /reports/sales renders the callouts + charts; the tab is a
real link; Overview/SKUs are unaffected."""
import itertools
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _order(db, dt, rev):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
                 order_type=OrderType.PAID, status="Completed", brand="smashbox",
                 gross_sales=Decimal(str(rev)), shipping_revenue=Decimal("0"),
                 seller_funded_outlandish=Decimal("0"), seller_funded_smashbox=Decimal("0"),
                 platform_discount_total=Decimal("0"), payment_platform_discount=Decimal("0")))
    db.flush()


def test_timing_tab_renders(client):
    with SessionLocal() as db:
        _order(db, datetime.now().replace(hour=12, minute=0, second=0, microsecond=0), 100)
        db.commit()
    r = client.get("/reports/sales?tab=timing")
    assert r.status_code == 200
    assert "Day of week" in r.text          # the DOW panel heading
    assert "Time of day" in r.text          # the hour panel heading
    assert "tab=timing" in r.text           # the tab is a real link carrying itself


def test_overview_default_unaffected(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text     # Overview content still renders


def test_skus_tab_still_works(client):
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "Showing" in r.text              # the SKU pagination control still renders
```

- [ ] **Step 2: Run to verify failure** — `py -m pytest tests/test_sales_timing_tab.py -v 2>&1 | tail -15`. Expected: `test_timing_tab_renders` FAILS (no Timing block; tab is a disabled span). The other two pass.

- [ ] **Step 3: Extend the route** — in `app/routers/reports.py`, in `sales_view`:

Change the tab normalization line:
```python
    ctx["tab"] = "skus" if tab == "skus" else "overview"
```
to:
```python
    ctx["tab"] = tab if tab in ("skus", "timing") else "overview"
```

Then, AFTER the existing `if ctx["tab"] == "skus": …` block (after its last line), add:
```python
    elif ctx["tab"] == "timing":
        from app.reports.temporal_patterns import compute_temporal_patterns
        from app.reports.dashboard_trends import bar_chart
        v = ctx["view"]
        t = compute_temporal_patterns(db, start=v.window_start, end=v.window_end)
        ctx["temporal"] = t
        ctx["dow_chart"] = bar_chart([float(d.avg_revenue) for d in t.dow])
        ctx["hour_chart"] = bar_chart([float(h.revenue) for h in t.hours])
        ctx["daily_chart"] = bar_chart([float(d.revenue) for d in t.daily])
```
(The `if ctx["tab"] == "skus":` becomes the head of an if/elif; do NOT change the SKU block's body.)

- [ ] **Step 4: Update the template** — `app/templates/reports/sales.html`. READ it first.

**(a) Fix the Overview content wrap** so it does NOT render on the Timing tab. Find `{% if tab != 'skus' %}` (the wrap around the summary cards + chart + velocity table) and change it to:
```html
{% if tab == 'overview' %}
```

**(b) Update the tab bar** (the `{# Report tabs … #}` block). Replace the three tab entries so Overview is active only on overview, and Timing is a real link:
```html
  <a href="/reports/sales?{{ period_qs }}&tab=overview"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab == 'overview' else 'border-transparent text-slate-500 hover:text-slate-800' }}">Overview</a>
  <a href="/reports/sales?{{ period_qs }}&tab=skus"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab == 'skus' else 'border-transparent text-slate-500 hover:text-slate-800' }}">SKUs</a>
  <a href="/reports/sales?{{ period_qs }}&tab=timing"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab == 'timing' else 'border-transparent text-slate-500 hover:text-slate-800' }}">Timing</a>
```
(Delete the old disabled `<span>…Timing…soon…</span>`.)

**(c) Add the Timing block** immediately before the final `{% endblock %}` (after the SKUs `{% endif %}`):
```html
{% if tab == 'timing' %}
{# ── Timing tab — aggregate temporal patterns ─────────────────────────── #}
{% set ti = temporal.insights %}
<section aria-label="Timing insights" class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">🗓️ Strongest day</div>
    <div class="mt-1 text-sm font-semibold text-slate-900">{% if ti.strongest_dow %}{{ ti.strongest_dow.label }}{% else %}—{% endif %}</div>
    <div class="text-[11px] text-slate-500">{% if ti.strongest_dow_pct is not none %}{{ '+' if ti.strongest_dow_pct >= 0 else '' }}{{ ti.strongest_dow_pct }}% vs daily avg{% endif %}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">⏰ Peak hour</div>
    <div class="mt-1 text-sm font-semibold text-slate-900">{% if ti.peak_hour_range %}{{ ti.peak_hour_range }}{% else %}—{% endif %}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">📈 Trend</div>
    <div class="mt-1 text-sm font-semibold text-slate-900">{{ ti.trend.label }}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">🔥 Best day</div>
    <div class="mt-1 text-sm font-semibold text-slate-900">{% if ti.best_day %}{{ ti.best_day.label }}{% else %}—{% endif %}</div>
    <div class="text-[11px] text-slate-500">{% if ti.best_day %}{{ ti.best_day.revenue | money }}{% endif %}</div>
  </div>
</section>

{# Day-of-week — avg revenue per occurrence #}
<section class="mb-5 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
  <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Day of week · avg revenue</div>
  {% set dow_tips = namespace(t=[]) %}
  {% for d in temporal.dow %}{% set dow_tips.t = dow_tips.t + [d.label ~ ": " ~ (d.avg_revenue | money)] %}{% endfor %}
  <div class="mt-2">{{ ui.barchart(dow_chart, tooltips=dow_tips.t, pos_tone="accent") }}</div>
  <div class="mt-1 grid grid-cols-7 text-center text-[11px]">
    {% for d in temporal.dow %}
    <div class="{{ 'font-semibold text-slate-900' if d.is_peak else 'text-slate-400' }}">{{ d.label }}</div>
    {% endfor %}
  </div>
</section>

{# Time of day — 24 hourly bars #}
<section class="mb-5 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
  <div class="flex items-baseline justify-between">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Time of day · revenue</div>
    {% if ti.peak_hour_range %}<div class="text-[10px] text-slate-400">peak {{ ti.peak_hour_range }}</div>{% endif %}
  </div>
  {% set hr_tips = namespace(t=[]) %}
  {% for h in temporal.hours %}{% set hr_tips.t = hr_tips.t + [h.label ~ ": " ~ (h.revenue | money)] %}{% endfor %}
  <div class="mt-2">{{ ui.barchart(hour_chart, tooltips=hr_tips.t, pos_tone="info") }}</div>
  <div class="mt-1 flex justify-between text-[11px] text-slate-400">
    <span>12a</span><span>6a</span><span>12p</span><span>6p</span><span>11p</span>
  </div>
  <div class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
    {% for dp in temporal.dayparts %}
    <span class="{{ 'font-semibold text-slate-900' if dp.is_peak else 'text-slate-500' }}">{{ dp.label }} {{ dp.revenue | money }}</span>
    {% endfor %}
  </div>
</section>

{# Trend / arc — daily revenue series #}
<section class="mb-5 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
  <div class="flex items-baseline justify-between">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Daily revenue · {{ temporal.window_start.strftime('%b %d') }}–{{ temporal.window_end.strftime('%b %d') }}</div>
    <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-700">{{ ti.trend.label }}</span>
  </div>
  {% set day_tips = namespace(t=[]) %}
  {% for d in temporal.daily %}{% set day_tips.t = day_tips.t + [d.label ~ ": " ~ (d.revenue | money)] %}{% endfor %}
  <div class="mt-2">{{ ui.barchart(daily_chart, tooltips=day_tips.t, pos_tone="pos") }}</div>
  <div class="mt-1 text-[11px] text-slate-500">{{ ti.trend.detail }}</div>
  {% if temporal.top_days %}
  <div class="mt-3 text-[11px] text-slate-500">
    <span class="font-semibold text-slate-700">Strongest days:</span>
    {% for d in temporal.top_days %}{{ d.label }} ({{ d.revenue | money }}){% if not loop.last %} · {% endif %}{% endfor %}
  </div>
  {% endif %}
</section>
{% endif %}
{% endblock %}
```
(Replace the existing final `{% endblock %}` — the new block ends with `{% endif %}` then `{% endblock %}`.)

- [ ] **Step 5: Run the tests**
- `py -m pytest tests/test_sales_timing_tab.py -v 2>&1 | tail -15` → all 3 pass.
- Regression: `py -m pytest tests/test_sales_skus_tab.py tests/test_sku_pagination.py tests/test_sales_page.py -q 2>&1 | tail -8` → all pass (Overview wrap change must not break SKUs/Overview).

- [ ] **Step 6: Commit** — `.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
timing: enable + render the Timing tab

Flip the Timing chip to a real tab; add a tab==timing branch to sales_view that
computes temporal patterns + builds the day-of-week / hour / daily bar charts;
add the Timing template block (insight callouts + 3 chart panels). Fix the
Overview wrap to render only on the overview tab (3-tab world).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py app/templates/reports/sales.html tests/test_sales_timing_tab.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`

---

## Task 3: Full suite + deploy + verify

- [ ] **Step 1:** `py -m pytest 2>&1 | tail -12` → all pass (883 + the new temporal/timing tests).
- [ ] **Step 2: Merge + deploy (local-merge, no PR):**
```bash
git push -u origin feature/temporal-patterns
git checkout main && git pull --ff-only
git merge --no-ff feature/temporal-patterns -m "Merge feature/temporal-patterns"
git push origin main
git branch -d feature/temporal-patterns && git push origin --delete feature/temporal-patterns
fly deploy
```
No schema change → release `alembic upgrade head` is a no-op.
- [ ] **Step 3: Verify** — `fly releases` healthy; `curl … /healthz` → 200; `curl … "/reports/sales?tab=timing"` → 303 (auth) / route registered. Then ask the user for the eyeball pass (desktop + phone): callouts populate, the three charts render, peak labels bold, trend label reads sensibly.

---

## Self-Review

**Spec coverage:** revenue by DOW avg-per-occurrence (`DowStat`, peak by avg) ✓; 24 hourly bars (`hours`, peak) ✓; dayparts note ✓; daily trend + shape label (`_trend_shape`, ±15% / CV 0.6 / ≥8 days) ✓; insight callouts (strongest weekday + % vs daily avg, peak hour range, trend, best day) ✓; revenue = velocity GMV (`_REV`) ✓; reuse `ui.barchart` (route builds the `BarChart`s) ✓; enable Timing tab + fix 3-tab active/wrap logic ✓; PAID-only, empty/short/single-day edge cases ✓.

**Placeholder scan:** none — full module + route + template + tests provided.

**Type consistency:** route context keys (`temporal`, `dow_chart`, `hour_chart`, `daily_chart`) match template refs; `temporal.insights` fields (`strongest_dow`, `strongest_dow_pct`, `peak_hour`, `peak_hour_range`, `best_day`, `trend.label`/`.detail`) used identically in the template; `DowStat.is_peak`/`label`/`avg_revenue`, `HourStat.label`/`revenue`, `DaypartStat.is_peak`/`label`/`revenue`, `DayStat.label`/`revenue` all match. `bar_chart(values)` → `ui.barchart(chart, tooltips, pos_tone)` signature matches `app/reports/dashboard_trends.py` + `partials/ui.html`. ✓

**Known simplification (documented, not a gap):** `ui.barchart` colors bars by sign only, so the per-bar peak is conveyed via the **bold under-chart label + the callout**, not a recolored bar — no shared-macro change. Charts use uniform tones (`accent`/`info`/`pos`).
