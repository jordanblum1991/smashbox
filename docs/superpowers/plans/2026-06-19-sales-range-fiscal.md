# Sales Report — Date Range + Fiscal Scopes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a custom date range (bucketed by the active Daily/Weekly/Monthly granularity) and fiscal scopes (Fiscal month → daily bars over the 29th–28th window; Fiscal YTD/year → fiscal-month bars) to the `/reports/sales` page.

**Architecture:** Refactor `compute_sales_report`'s window/aggregate logic into a shared `_summarize(defs, key_of, …)` core, then drive it from three plans — calendar (daily/weekly/monthly, optional custom `[start,end]`), fiscal-month-as-daily, and fiscal-month-buckets — reusing `app/reports/fiscal_calendar.py`. The route + template mirror the Ad-Spend control bar (Fiscal ▾ `<details>`, date-range form, year/month picker, `fiscal_banner.html`).

**Tech Stack:** FastAPI/Starlette, Jinja2 + `partials/ui.html`/`fiscal_banner.html`, SQLAlchemy 2.x, pytest. Spec: `docs/superpowers/specs/2026-06-19-sales-range-fiscal-design.md`.

**Branch:** `feature/sales-range-fiscal` (created; spec committed).

**Conventions:**
- Run tests via Bash: `py -m pytest <path> -v 2>&1 | tail -30` (NOT PowerShell/venv).
- Commit msgs: write to `.git/COMMIT_MSG_DRAFT.txt`, then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Money is `Decimal`. Auth disabled in tests. Starlette: `templates.TemplateResponse(request, "x.html", {...})`.

---

## File Structure

- **Modify** `app/reports/sales_report.py` — refactor to `_summarize` core; add `start`/`end` params (Task 1) and `FISCAL_MODES` + fiscal dispatch + `current_fiscal_ym` (Task 2).
- **Modify** `app/routers/reports.py` — extend `sales_view` + `sales_csv` for scope/range/fiscal (Task 3).
- **Modify** `app/templates/reports/sales.html` — Fiscal ▾ dropdown, date-range form, year/month picker, fiscal banner (Task 4).
- **Modify** `tests/test_sales_report.py` (Tasks 1–2) and `tests/test_sales_page.py` (Task 3–4).

---

## Task 1: Refactor to a shared core + custom date range

**Files:**
- Modify: `app/reports/sales_report.py`
- Test: `tests/test_sales_report.py`

- [ ] **Step 1: Write the failing range tests**

Append to `tests/test_sales_report.py` (the `_seed` helper + imports already exist):

```python
def test_custom_range_limits_buckets_daily():
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 20), gross=50, units=1)   # outside the range
        db.commit()
        view = compute_sales_report(db, "daily",
                                    start=date(2026, 3, 1), end=date(2026, 3, 31),
                                    as_of=date(2026, 6, 1))
    assert view.window_start == date(2026, 3, 1)
    assert view.window_end == date(2026, 3, 31)
    keys = [b.key for b in view.buckets]
    assert "2026-03-10" in keys
    assert "2026-05-20" not in keys                      # excluded by the range
    assert view.total_revenue == Decimal("100.00")       # only the in-range order


def test_custom_range_weekly_buckets_the_span():
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 3), gross=100, units=1)
        _seed(db, date(2026, 3, 17), gross=40, units=1)
        db.commit()
        view = compute_sales_report(db, "weekly",
                                    start=date(2026, 3, 1), end=date(2026, 3, 28),
                                    as_of=date(2026, 6, 1))
    # All buckets are Mondays within/under the span; revenue lands in two of them.
    assert view.total_revenue == Decimal("140.00")
    assert all(b.start.weekday() == 0 for b in view.buckets)   # weekly = Mondays
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_report.py -k custom_range -v 2>&1 | tail -15`
Expected: FAIL — `compute_sales_report() got an unexpected keyword argument 'start'`.

- [ ] **Step 3: Refactor `compute_sales_report` into a shared core + add range**

In `app/reports/sales_report.py`, add `from dataclasses import dataclass` if not present (it is — `SalesBucket` uses it). Replace the single `compute_sales_report` function (lines ~120–206) with the following. Keep all existing helpers (`_window_for`, `_bucket_start`, `_add_months`, `_month_floor`, `_label`, `_key`, `_span_starts`) UNCHANGED above it.

```python
@dataclass
class _BucketDef:
    key: str
    label: str
    start: date


def _calendar_plan(granularity: str, win_start: date, win_end: date):
    """Bucket defs + a date→key mapper for daily/weekly/monthly over the window."""
    defs = [_BucketDef(_key(granularity, s), _label(granularity, s), s)
            for s in _span_starts(granularity, win_start, win_end)]

    def key_of(d: date) -> str:
        return _key(granularity, _bucket_start(granularity, d))

    return defs, key_of


def _summarize(db: Session, defs: list[_BucketDef], key_of,
               win_start: date, win_end: date, granularity_value: str,
               today: date) -> SalesReportView:
    """Seed the given buckets, sum PAID orders in [win_start, win_end] into them
    via key_of(placed_local_date), and compute totals / deltas / peak. The single
    aggregation core shared by every scope so they can't drift."""
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
                         as_of: date | None = None) -> SalesReportView:
    today = as_of or today_local()
    if granularity not in GRANULARITIES:
        granularity = "daily"
    if start is not None and end is not None:
        win_start, win_end = start, end
    else:
        win_start, win_end = _window_for(granularity, today)
    defs, key_of = _calendar_plan(granularity, win_start, win_end)
    return _summarize(db, defs, key_of, win_start, win_end, granularity, today)
```

(Task 2 adds the fiscal branch + extra kwargs to this same `compute_sales_report`.)

- [ ] **Step 4: Run the new + existing compute tests**

Run: `py -m pytest tests/test_sales_report.py -v 2>&1 | tail -25`
Expected: all pass — the 8 existing tests (behavior preserved by the refactor) + the 2 new range tests = 10 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: shared _summarize core + custom date range

Refactor compute_sales_report's window/aggregate logic into a reusable
_summarize(defs, key_of, …) core driven by a bucket "plan", and add optional
start/end params so a custom range is bucketed by the active granularity.
Behavior for the default trailing windows is unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/sales_report.py tests/test_sales_report.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: Fiscal scopes (month → daily, YTD/year → fiscal-month buckets)

**Files:**
- Modify: `app/reports/sales_report.py`
- Test: `tests/test_sales_report.py`

- [ ] **Step 1: Write the failing fiscal tests**

Append to `tests/test_sales_report.py`:

```python
def test_fiscal_month_renders_daily_bars_over_2928_window():
    # Fiscal May 2026 = Apr 29 – May 28. Daily bars across that window.
    with SessionLocal() as db:
        _seed(db, date(2026, 4, 29), gross=100, units=1)   # first day of fiscal May
        _seed(db, date(2026, 5, 28), gross=70, units=1)    # last day of fiscal May
        _seed(db, date(2026, 5, 29), gross=999, units=1)   # next fiscal month — excluded
        db.commit()
        view = compute_sales_report(db, "fiscal_month",
                                    fiscal_year=2026, fiscal_month=5, as_of=date(2026, 6, 1))
    assert view.window_start == date(2026, 4, 29)
    assert view.window_end == date(2026, 5, 28)
    keys = [b.key for b in view.buckets]
    assert "2026-04-29" in keys and "2026-05-28" in keys   # daily buckets
    assert "2026-05-29" not in keys
    assert view.total_revenue == Decimal("170.00")         # 100 + 70, not 999


def test_fiscal_year_has_twelve_fiscal_month_buckets():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)   # lands in fiscal May
        db.commit()
        view = compute_sales_report(db, "fiscal_year",
                                    fiscal_year=2026, as_of=date(2026, 12, 31))
    assert len(view.buckets) == 12
    may = next(b for b in view.buckets if b.key == "F2026-05")
    assert may.label == "May 2026"
    assert may.revenue == Decimal("100.00")


def test_fiscal_ytd_buckets_jan_through_month():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)
        db.commit()
        view = compute_sales_report(db, "fiscal_ytd",
                                    fiscal_year=2026, fiscal_month=6, as_of=date(2026, 6, 30))
    assert len(view.buckets) == 6                           # fiscal Jan..Jun
    assert [b.key for b in view.buckets][0] == "F2026-01"
    assert [b.key for b in view.buckets][-1] == "F2026-06"


def test_fiscal_year_month_bucket_ties_to_fiscal_month_scope_total():
    """The fiscal-month bucket inside the year view equals the fiscal_month scope's
    own total over the same 29th–28th window (the two fiscal paths can't drift)."""
    with SessionLocal() as db:
        _seed(db, date(2026, 4, 29), gross=200, units=2, shipping_revenue=10,
              seller_funded_outlandish=5)
        _seed(db, date(2026, 5, 28), gross=120, units=1, platform_discount_total=8)
        db.commit()
        year_view = compute_sales_report(db, "fiscal_year", fiscal_year=2026,
                                         as_of=date(2026, 12, 31))
        month_view = compute_sales_report(db, "fiscal_month", fiscal_year=2026,
                                          fiscal_month=5, as_of=date(2026, 12, 31))
    may_bucket = next(b for b in year_view.buckets if b.key == "F2026-05")
    assert may_bucket.revenue == month_view.total_revenue


def test_current_fiscal_ym_maps_day_after_28_to_next_month():
    assert current_fiscal_ym(date(2026, 5, 28)) == (2026, 5)
    assert current_fiscal_ym(date(2026, 5, 29)) == (2026, 6)
    assert current_fiscal_ym(date(2026, 12, 31)) == (2027, 1)
```

Add `current_fiscal_ym` and `FISCAL_MODES` to the import line at the top of the test:
```python
from app.reports.sales_report import FISCAL_MODES, compute_sales_report, current_fiscal_ym
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_report.py -k "fiscal or current_fiscal" -v 2>&1 | tail -20`
Expected: FAIL — `cannot import name 'current_fiscal_ym'` / `FISCAL_MODES`.

- [ ] **Step 3: Add the fiscal machinery**

In `app/reports/sales_report.py`, add the import near the top (with the other `from app...` imports):
```python
import calendar as _calendar
from app.reports.fiscal_calendar import fiscal_months_for, fiscal_window
```

Add after `GRANULARITIES = (...)`:
```python
FISCAL_MODES = ("fiscal_month", "fiscal_ytd", "fiscal_year")
```

Add these helpers (anywhere below `_calendar_plan`):
```python
def current_fiscal_ym(today: date) -> tuple[int, int]:
    """(fiscal_year, fiscal_month) containing `today`. A fiscal month closes on
    the 28th, so days 1–28 belong to that calendar month's fiscal period and
    days 29–31 roll into the next fiscal month (which may cross the year)."""
    if today.day <= 28:
        return today.year, today.month
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def _fiscal_month_plan(fiscal_year: int, months: list[int]):
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
```

Then add a fiscal branch at the TOP of `compute_sales_report` (before the calendar logic):
```python
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
    if start is not None and end is not None:
        win_start, win_end = start, end
    else:
        win_start, win_end = _window_for(granularity, today)
    defs, key_of = _calendar_plan(granularity, win_start, win_end)
    return _summarize(db, defs, key_of, win_start, win_end, granularity, today)
```

- [ ] **Step 4: Run the fiscal tests + the whole file**

Run: `py -m pytest tests/test_sales_report.py -v 2>&1 | tail -30`
Expected: all pass (10 from Task 1 + 5 new fiscal = 15 passed).

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: fiscal scopes (month→daily, YTD/year→fiscal-month buckets)

Add FISCAL_MODES + fiscal dispatch to compute_sales_report: Fiscal month
renders daily bars across the 29th–28th window; Fiscal YTD/year render one
bucket per fiscal month (labeled by closing month). Reuses fiscal_calendar;
current_fiscal_ym maps a date to its fiscal period.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/sales_report.py tests/test_sales_report.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: Route + CSV (scope / range / fiscal params + banner)

**Files:**
- Modify: `app/routers/reports.py`
- Test: `tests/test_sales_page.py`

- [ ] **Step 1: Write the failing route tests**

Append to `tests/test_sales_page.py`:

```python
def test_custom_range_scopes_the_page(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-01&end_date=2026-03-31")
    assert r.status_code == 200
    assert "Mar 01" in r.text or "Mar 1" in r.text       # window label reflects the range


def test_bad_range_shows_error_and_falls_back(client):
    r = client.get("/reports/sales?granularity=daily&start_date=2026-03-10&end_date=2026-03-01")
    assert r.status_code == 200
    assert "Start date must be on or before end date" in r.text


def test_fiscal_month_scope_renders_banner(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales?granularity=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert "Fiscal" in r.text                              # fiscal banner / label
    # banner absent on the plain daily view
    assert "Fiscal May 2026" not in client.get("/reports/sales").text


def test_fiscal_year_csv_exports(client):
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), 100, 1)
        db.commit()
    r = client.get("/reports/sales.csv?granularity=fiscal_year&year=2026")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "fiscal_year" in r.headers["content-disposition"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py -k "range or fiscal" -v 2>&1 | tail -15`
Expected: FAIL (no banner / no range handling / 422 on unknown params).

- [ ] **Step 3: Rewrite the route + CSV**

In `app/routers/reports.py`, update the import:
```python
from app.reports.sales_report import (
    FISCAL_MODES, GRANULARITIES, compute_sales_report, current_fiscal_ym,
)
```
(`fiscal_banner_payload` is already imported at the top of reports.py.)

Replace the `sales_view` route with:
```python
def _sales_view_data(db, granularity, start_date, end_date, year, month):
    """Resolve a sales report + its template context for the given query scope.
    Shared by the page and the CSV so they never diverge."""
    today = today_local()
    error = None
    fiscal_banner = fy = fm = None

    if granularity in FISCAL_MODES:
        cur_y, cur_m = current_fiscal_ym(today)
        fy, fm = year or cur_y, month or cur_m
        view = compute_sales_report(db, granularity, fiscal_year=fy, fiscal_month=fm)
        fiscal_banner = fiscal_banner_payload(granularity, fy, fm)
    else:
        if granularity not in GRANULARITIES:
            granularity = "daily"
        start = end = None
        if start_date and end_date:
            try:
                start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
                if start > end:
                    error, start, end = "Start date must be on or before end date.", None, None
            except ValueError:
                error, start, end = "Dates must be in YYYY-MM-DD format.", None, None
        view = compute_sales_report(db, granularity, start=start, end=end)

    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    cur_y, _ = current_fiscal_ym(today)
    return {
        "view": view, "granularities": GRANULARITIES, "granularity": granularity,
        "window_label": window_label, "chart": bar_chart([float(b.revenue) for b in view.buckets]),
        "start_date": start_date or "", "end_date": end_date or "",
        "fiscal_banner": fiscal_banner, "fiscal_year": fy, "fiscal_month": fm,
        "fiscal_years": list(range(cur_y - 2, cur_y + 1)), "error": error,
    }


@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily",
               start_date: str | None = None, end_date: str | None = None,
               year: int | None = None, month: int | None = None,
               db: Session = Depends(get_db)):
    """Sales velocity — calendar (daily/weekly/monthly, optional custom range) or
    fiscal (month→daily, YTD/year→fiscal-month) scopes."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    return templates.TemplateResponse(request, "reports/sales.html", ctx)
```

Replace the `sales_csv` route with:
```python
@router.get("/reports/sales.csv")
def sales_csv(granularity: str = "daily",
              start_date: str | None = None, end_date: str | None = None,
              year: int | None = None, month: int | None = None,
              db: Session = Depends(get_db)) -> Response:
    """Sales velocity table as CSV (mirrors the on-screen scope)."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    view = ctx["view"]

    def rows():
        for b in view.buckets:
            yield [b.label, b.start.isoformat(), f"{b.revenue:.2f}",
                   b.units, b.orders, f"{b.aov:.2f}", "yes" if b.in_progress else ""]

    if granularity in FISCAL_MODES:
        suffix = f"{granularity}_{ctx['fiscal_year']}"
    elif ctx["start_date"] and ctx["end_date"] and not ctx["error"]:
        suffix = f"{ctx['start_date']}_to_{ctx['end_date']}"
    else:
        suffix = view.granularity
    return _csv_response(
        rows(),
        ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"],
        f"sales_{suffix}.csv",
    )
```

- [ ] **Step 4: Run the route tests + the existing sales-page tests**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -25`
Expected: all pass (the existing 8 + 4 new = 12 passed). The `_sales_view_data` helper makes both routes consistent.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: route + CSV for date-range and fiscal scopes

Parse granularity (calendar + fiscal_*), start_date/end_date, year/month; share
one _sales_view_data resolver between the page and CSV; surface a range error
and the fiscal accent banner. CSV filename encodes the scope.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: Template — Fiscal ▾ dropdown + date-range form + banner

**Files:**
- Modify: `app/templates/reports/sales.html`
- Test: `tests/test_sales_page.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_sales_page.py`:
```python
def test_controls_render_fiscal_dropdown_and_range_form(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Fiscal" in r.text                                   # Fiscal ▾ dropdown
    assert 'name="start_date"' in r.text and 'name="end_date"' in r.text   # range form
    assert "scope=fiscal_month" in r.text or "granularity=fiscal_month" in r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py::test_controls_render_fiscal_dropdown_and_range_form -v 2>&1 | tail -12`
Expected: FAIL — no fiscal dropdown / range form yet.

- [ ] **Step 3: Update the template controls**

In `app/templates/reports/sales.html`, add the fiscal-banner import at the top (after the `ui` import):
```html
{% import "partials/fiscal_banner.html" as fb %}
```

Replace the existing granularity-toggle block:
```html
{# Granularity toggle #}
<div class="mb-4 inline-flex overflow-hidden rounded-lg border border-slate-200 print:hidden">
  {% for g in granularities %}
  <a href="/reports/sales?granularity={{ g }}"
     class="px-3.5 py-1.5 text-sm font-medium {{ 'bg-slate-900 text-white' if g == view.granularity else 'text-slate-600 hover:bg-slate-100' }}">
    {{ g | capitalize }}
  </a>
  {% endfor %}
</div>
```
with this control bar (toggle carries the range; Fiscal ▾ dropdown; range form; fiscal year/month picker; banner):
```html
{% set is_fiscal = granularity in ('fiscal_month', 'fiscal_ytd', 'fiscal_year') %}
{# Scope controls — calendar toggle + Fiscal dropdown (mirrors Ad Spend). #}
<div class="mb-4 flex flex-wrap items-center gap-1 text-sm print:hidden">
  {% for g in granularities %}
  <a href="/reports/sales?granularity={{ g }}{% if start_date and end_date %}&start_date={{ start_date }}&end_date={{ end_date }}{% endif %}"
     class="rounded-md px-4 py-1.5 font-medium {{ 'bg-slate-900 text-white' if g == granularity else 'text-slate-600 hover:bg-slate-100' }}">{{ g | capitalize }}</a>
  {% endfor %}
  {# Fiscal periods (29th–28th) behind a dropdown to keep the bar tidy. #}
  <details class="group relative">
    <summary class="flex cursor-pointer list-none items-center gap-1 rounded-md px-4 py-1.5 font-medium {{ 'bg-slate-900 text-white' if is_fiscal else 'text-slate-600 hover:bg-slate-100' }}">
      Fiscal <span class="text-[10px] transition-transform group-open:rotate-180">▾</span>
    </summary>
    <div class="absolute left-0 top-full z-20 mt-1 w-40 rounded-md border border-slate-200 bg-white p-1 shadow-lg">
      <a href="/reports/sales?granularity=fiscal_month" class="block rounded-md px-3 py-1.5 {{ 'bg-slate-100 font-medium text-slate-900' if granularity == 'fiscal_month' else 'text-slate-700 hover:bg-slate-50' }}">Fiscal month</a>
      <a href="/reports/sales?granularity=fiscal_ytd" class="block rounded-md px-3 py-1.5 {{ 'bg-slate-100 font-medium text-slate-900' if granularity == 'fiscal_ytd' else 'text-slate-700 hover:bg-slate-50' }}">Fiscal YTD</a>
      <a href="/reports/sales?granularity=fiscal_year" class="block rounded-md px-3 py-1.5 {{ 'bg-slate-100 font-medium text-slate-900' if granularity == 'fiscal_year' else 'text-slate-700 hover:bg-slate-50' }}">Fiscal year</a>
    </div>
  </details>
</div>

{% if error %}<div class="mb-3 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 print:hidden">{{ error }}</div>{% endif %}

{% if not is_fiscal %}
{# Custom date range — buckets the span by the active granularity. #}
<form method="get" action="/reports/sales" class="mb-5 flex flex-wrap items-center gap-2 text-sm print:hidden">
  <input type="hidden" name="granularity" value="{{ granularity }}">
  <span class="text-xs text-slate-500">Range</span>
  <input type="text" name="start_date" value="{{ start_date }}" placeholder="YYYY-MM-DD"
         class="w-32 rounded-md border border-slate-300 px-2 py-1">
  <span class="text-slate-400">→</span>
  <input type="text" name="end_date" value="{{ end_date }}" placeholder="YYYY-MM-DD"
         class="w-32 rounded-md border border-slate-300 px-2 py-1">
  <button type="submit" class="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white">Apply</button>
  {% if start_date and end_date %}<a href="/reports/sales?granularity={{ granularity }}" class="text-slate-500 hover:text-slate-700">Clear</a>{% endif %}
</form>
{% else %}
{# Fiscal year/month picker (month disabled for the fiscal-year scope). #}
<form method="get" action="/reports/sales" class="mb-5 flex flex-wrap items-center gap-2 text-sm print:hidden">
  <input type="hidden" name="granularity" value="{{ granularity }}">
  <span class="text-xs text-slate-500">Fiscal {{ 'year' if granularity == 'fiscal_year' else ('YTD through' if granularity == 'fiscal_ytd' else 'month') }}</span>
  <select name="year" onchange="this.form.submit()" class="rounded-md border border-slate-300 px-2 py-1">
    {% for y in fiscal_years %}<option value="{{ y }}" {% if y == fiscal_year %}selected{% endif %}>{{ y }}</option>{% endfor %}
  </select>
  <select name="month" onchange="this.form.submit()" {% if granularity == 'fiscal_year' %}disabled{% endif %} class="rounded-md border border-slate-300 px-2 py-1">
    {% for mm in range(1, 13) %}<option value="{{ mm }}" {% if mm == fiscal_month %}selected{% endif %}>{{ mm | month_short }}</option>{% endfor %}
  </select>
  <noscript><button type="submit" class="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white">Apply</button></noscript>
</form>
{{ fb.fiscal_banner(fiscal_banner) }}
{% endif %}
```

NOTE: this relies on `partials/fiscal_banner.html` exposing a `fiscal_banner(payload)` macro (it does — `ad_spend.html` uses `{% import "partials/fiscal_banner.html" as fb %}` + `{{ fb.fiscal_banner(fiscal_banner) }}`) and the `month_short` filter (used by `ad_spend.html`). If either is missing, check how `ad_spend.html` references it and match exactly.

- [ ] **Step 4: Run the template + page tests**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -25`
Expected: all pass (13 — the 12 from Task 3 + the controls test).

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: Fiscal ▾ dropdown + date-range form + fiscal banner

Mirror the Ad-Spend control bar: calendar toggle (carrying any active range),
a Fiscal ▾ dropdown (month/YTD/year), a custom date-range form for the calendar
scopes, a fiscal year/month picker, and the shared fiscal accent banner.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/reports/sales.html tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 5: Full suite + deploy

**Files:** none (verification + ship)

- [ ] **Step 1: Full suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 824 + the new sales tests; 11 skipped).

- [ ] **Step 2: Local visual smoke (optional)**

Boot uvicorn, load `/reports/sales`, exercise: a custom range (daily + weekly), Fiscal month (daily bars Apr 29–May 28), Fiscal YTD, Fiscal year (12 fiscal-month bars), and confirm the banner + Download-CSV reflect each scope.

- [ ] **Step 3: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/sales-range-fiscal
git checkout main && git pull --ff-only
git merge --no-ff feature/sales-range-fiscal -m "Merge feature/sales-range-fiscal"
git push origin main
git branch -d feature/sales-range-fiscal && git push origin --delete feature/sales-range-fiscal
fly deploy
```
No schema change → the release `alembic upgrade head` is a no-op.

- [ ] **Step 4: Verify on prod**

`fly releases` healthy; load `https://smashbox.fly.dev/reports/sales`, confirm the range form + Fiscal ▾ work and CSV downloads per scope. Then ask the user for the authoritative eyeball pass.

---

## Self-Review

**Spec coverage:**
- Custom range works with granularity toggle → Task 1 (`start`/`end`) + Task 3 (parse) + Task 4 (form, toggle carries range). ✓
- Fiscal month → daily bars over 29th–28th → Task 2 (`fiscal_month` via `_calendar_plan("daily", fiscal_window)`). ✓
- Fiscal YTD/year → fiscal-month bars → Task 2 (`_fiscal_month_plan`). ✓
- Fiscal accent banner → Task 3 (`fiscal_banner_payload`) + Task 4 (`fb.fiscal_banner`). ✓
- `compute_sales_report` gains start/end/fiscal_year/fiscal_month; shared summation core → Tasks 1–2 (`_summarize`). ✓
- Chart/cards/table/CSV render off `view.buckets` unchanged; CSV matches scope → Task 3 (shared `_sales_view_data`, scope-encoded filename). ✓
- Bad range → inline error + default window → Task 3 (`error`) + Task 4 (error slot). ✓
- Parity (fiscal bucket ties to fiscal window) → Task 2 (`..._ties_to_fiscal_month_scope_total`); calendar revenue formula already tied to `MonthlyPnL.gmv` by the existing test. ✓
- Out-of-scope (fiscal weekly/daily beyond month-drill, saved ranges) honored. ✓

**Placeholder scan:** No TBD/TODO; every code step is complete. The fiscal-banner/`month_short` reuse is verified against `ad_spend.html` with a fallback instruction.

**Type consistency:** `compute_sales_report(db, granularity, *, start, end, fiscal_year, fiscal_month, as_of)`, `current_fiscal_ym(date) -> (int,int)`, `FISCAL_MODES`, `_BucketDef(key,label,start)`, `_calendar_plan`/`_fiscal_month_plan` returning `(defs, key_of)`, and `_summarize(db, defs, key_of, win_start, win_end, granularity_value, today)` are used identically across Tasks 1–3. Bucket keys: calendar `_key` (iso / "YYYY-MM"), fiscal `"F{year}-{mm:02d}"` — consistent between Task 2 and its tests. Context keys (`granularity`, `start_date`, `end_date`, `fiscal_banner`, `fiscal_year`, `fiscal_month`, `fiscal_years`, `error`) match between Task 3's `_sales_view_data` and Task 4's template. ✓
