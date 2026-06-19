# Sales Velocity Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing-but-orphaned `compute_sales_report` into a top-level **Sales** page (summary cards + inline-SVG revenue bar chart + velocity table + granularity toggle + CSV export), and remove the module's dead helpers.

**Architecture:** Reuse `app/reports/sales_report.py::compute_sales_report` unchanged (it already returns a `SalesReportView`). Add two routes to `app/routers/reports.py` (`/reports/sales` + `/reports/sales.csv`), a `reports/sales.html` template using the shared `ui` macros (`page_header`, `barchart`, `delta_chip`) + the `money` filter, and a top-level nav link. The bar chart reuses `dashboard_trends.bar_chart`; the CSV reuses the `_csv_response` helper.

**Tech Stack:** FastAPI/Starlette, Jinja2 + `partials/ui.html` macros, SQLAlchemy 2.x, pytest. Spec: `docs/superpowers/specs/2026-06-19-sales-velocity-report-design.md`.

**Branch:** `feature/sales-velocity-report` (created; spec committed).

**Conventions for the implementer:**
- Run tests via the Bash tool: `py -m pytest <path> -v 2>&1 | tail -25` (NOT PowerShell, NOT venv pytest).
- Commit messages: write to `.git/COMMIT_MSG_DRAFT.txt` with the Write tool, then `git commit -F .git/COMMIT_MSG_DRAFT.txt`. End every message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Money is `Decimal`; the `money` Jinja filter formats it (e.g. `{{ x | money }}`).
- Auth is disabled in tests (TestClient needs no login).
- Starlette template call form: `templates.TemplateResponse(request, "name.html", {...})` — `request` is the first positional arg.

---

## File Structure

- **Modify** `app/reports/sales_report.py` — remove dead `_bucket_days` + `_is_weekly_key`.
- **Modify** `app/routers/reports.py` — add `/reports/sales` (GET) + `/reports/sales.csv` (GET), and one import line.
- **Create** `app/templates/reports/sales.html` — the page.
- **Modify** `app/templates/partials/nav.html` — add the top-level "Sales" link.
- **Create** `tests/test_sales_report.py` — compute-layer characterization + parity tests.
- **Create** `tests/test_sales_page.py` — route + CSV + nav tests.

---

## Task 1: Characterization tests + remove dead helpers

The compute function already works, so these tests **pass immediately** — they characterize current behavior and lock it in before we delete dead code (and the parity test verifies the "ties to the dashboard" claim).

**Files:**
- Create: `tests/test_sales_report.py`
- Modify: `app/reports/sales_report.py`

- [ ] **Step 1: Write the characterization + parity tests**

```python
# tests/test_sales_report.py
"""compute_sales_report: bucketing per granularity, the GMV revenue formula,
trend-delta excludes the in-progress bucket, peak, empty window, and parity
with MonthlyPnL.gmv. Seeds PAID orders; no network."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.reports.sales_report import compute_sales_report

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed(db, d: date, *, gross, units, order_type=OrderType.PAID, **money):
    """One order placed at noon on day `d` (noon avoids tz-shift day crossing),
    with `units` total via a single OrderLine, plus optional discount/ship money
    fields (shipping_revenue, seller_funded_outlandish, seller_funded_smashbox,
    platform_discount_total, payment_platform_discount)."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross)),
              **{k: Decimal(str(v)) for k, v in money.items()})
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="X", quantity=units))
    db.flush()
    return o


def test_daily_buckets_revenue_units_orders():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=2)
        _seed(db, date(2026, 5, 10), gross=40, units=1)   # same day -> same bucket
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 15))
    bucket = next(b for b in view.buckets if b.key == "2026-05-10")
    assert bucket.revenue == Decimal("140.00")   # 100 + 40, no discounts
    assert bucket.units == 3                       # 2 + 1
    assert bucket.orders == 2
    assert view.total_orders == 2


def test_revenue_applies_gmv_formula():
    with SessionLocal() as db:
        # gross 100 + ship 10 - out 5 - smash 3 - platform 4 - pay 2 = 96
        _seed(db, date(2026, 5, 10), gross=100, units=1, shipping_revenue=10,
              seller_funded_outlandish=5, seller_funded_smashbox=3,
              platform_discount_total=4, payment_platform_discount=2)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert next(b for b in view.buckets if b.key == "2026-05-10").revenue == Decimal("96.00")


def test_monthly_rolls_up_within_a_month():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 3), gross=100, units=1)
        _seed(db, date(2026, 5, 20), gross=50, units=1)
        db.commit()
        view = compute_sales_report(db, "monthly", as_of=date(2026, 5, 25))
    may = next(b for b in view.buckets if b.key == "2026-05")
    assert may.revenue == Decimal("150.00")
    assert may.orders == 2


def test_samples_excluded():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 10), gross=0, units=1, order_type=OrderType.SAMPLE)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert view.total_orders == 1
    assert view.total_revenue == Decimal("100.00")


def test_trend_delta_excludes_in_progress_bucket():
    # daily: today = 2026-05-15 (in-progress). Prior two complete days carry the delta.
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 13), gross=100, units=1)   # prior-complete
        _seed(db, date(2026, 5, 14), gross=150, units=1)   # last-complete
        _seed(db, date(2026, 5, 15), gross=999, units=1)   # in-progress (excluded)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 15))
    # delta compares 14th (150) vs 13th (100) -> +50%, NOT touching the 999.
    assert view.revenue_delta is not None
    assert view.revenue_delta.state == "up"
    assert "50" in view.revenue_delta.label


def test_peak_is_highest_revenue_bucket_and_none_when_empty():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 11), gross=300, units=1)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert view.peak is not None and view.peak.key == "2026-05-11"

    with SessionLocal() as db:
        empty = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert empty.peak is None
    assert empty.total_revenue == Decimal("0.00")
    assert empty.revenue_delta is None
    assert len(empty.buckets) == 30          # window still fully seeded with zeros


def test_monthly_revenue_ties_to_monthly_pnl_gmv():
    """The page's headline claim: monthly sales revenue == MonthlyPnL.gmv."""
    from app.reports.monthly_pnl import compute_monthly_pnl
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 8), gross=200, units=2, shipping_revenue=15,
              seller_funded_outlandish=10, platform_discount_total=8)
        _seed(db, date(2026, 5, 18), gross=120, units=1, seller_funded_smashbox=6,
              payment_platform_discount=3)
        db.commit()
        view = compute_sales_report(db, "monthly", as_of=date(2026, 5, 25))
        may = next(b for b in view.buckets if b.key == "2026-05")
        pnl = compute_monthly_pnl(db, 2026, 5)
    assert may.revenue == pnl.gmv
```

- [ ] **Step 2: Run the tests — they should PASS against the existing compute function**

Run: `py -m pytest tests/test_sales_report.py -v 2>&1 | tail -30`
Expected: 7 passed. (If `test_monthly_revenue_ties_to_monthly_pnl_gmv` FAILS, do NOT weaken it — report it: it means the revenue formula diverged from `MonthlyPnL.gmv` and the spec's parity claim needs revisiting.)

- [ ] **Step 3: Remove the dead helpers**

In `app/reports/sales_report.py`, delete the entire `_bucket_days` function (the `def _bucket_days(b: SalesBucket) -> int:` block) and the entire `_is_weekly_key` function (`def _is_weekly_key(key: str) -> bool:` block). They are defined but never called by `compute_sales_report` (confirmed: only `_bucket_days` referenced `_is_weekly_key`, and nothing calls `_bucket_days`). Leave everything else untouched.

- [ ] **Step 4: Re-run the tests + a quick import check**

Run: `py -c "import app.reports.sales_report" 2>&1 | tail -2 && py -m pytest tests/test_sales_report.py -q 2>&1 | tail -5`
Expected: import OK; 7 passed.

- [ ] **Step 5: Commit**

Write `.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: characterization tests + drop dead helpers

Lock compute_sales_report behavior (bucketing, GMV revenue formula, trend
delta, peak, empty window, parity with MonthlyPnL.gmv) and remove the unused
_bucket_days / _is_weekly_key helpers (the latter an admitted stub).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/sales_report.py tests/test_sales_report.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: `/reports/sales` route + page (cards, toggle, table)

**Files:**
- Modify: `app/routers/reports.py`
- Create: `app/templates/reports/sales.html`
- Test: `tests/test_sales_page.py`

- [ ] **Step 1: Write the failing route test**

```python
# tests/test_sales_page.py
"""Sales page: renders per granularity, toggle switches the window, invalid
granularity falls back to daily, CSV exports the velocity table, nav links it."""
import csv
import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db, d, gross, units):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{d.isoformat()}-{gross}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="X", quantity=units))
    db.flush()


def test_sales_page_renders():
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 2)
        db.commit()
    r = TestClient(app).get("/reports/sales")
    assert r.status_code == 200
    assert "Sales" in r.text
    assert "Daily" in r.text and "Weekly" in r.text and "Monthly" in r.text


def test_granularity_toggle_switches_view(client):
    r = client.get("/reports/sales?granularity=monthly")
    assert r.status_code == 200
    # the monthly toggle link is marked active; the CSV/links carry the granularity
    assert "granularity=monthly" in r.text


def test_invalid_granularity_falls_back_to_daily(client):
    r = client.get("/reports/sales?granularity=foo")
    assert r.status_code == 200            # no crash; compute coerces to daily


def test_no_data_renders_empty_state(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200            # zero buckets still render
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -20`
Expected: FAIL — 404 (route not defined yet).

- [ ] **Step 3: Add the route to `app/routers/reports.py`**

Add this import near the other `from app.reports.* import` lines (e.g. after the `pnl` import):
```python
from app.reports.sales_report import GRANULARITIES, compute_sales_report
```

Add the route (place it after the existing `ytd_pnl_legacy` route near line 168, before the `samples` route):
```python
@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily", db: Session = Depends(get_db)):
    """Sales velocity — revenue/units/orders per day/week/month with trend."""
    view = compute_sales_report(db, granularity)
    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    return templates.TemplateResponse(
        request, "reports/sales.html",
        {"view": view, "granularities": GRANULARITIES, "window_label": window_label},
    )
```

- [ ] **Step 4: Create `app/templates/reports/sales.html`**

```html
{% extends "base.html" %}
{% import "partials/ui.html" as ui %}
{% block title %}Sales · Smashbox{% endblock %}

{% block content %}
{% call ui.page_header(
  eyebrow="Smashbox TikTok Shop",
  title="Sales",
  period=window_label,
  subtitle="PAID orders · Seller-Center GMV",
  accent_bar=true,
) %}
  <a href="/reports/sales.csv?granularity={{ view.granularity }}"
     class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50">
    {{ ui.icon("download", "h-3.5 w-3.5") }}
    Download CSV
  </a>
{% endcall %}

{# Granularity toggle #}
<div class="mb-4 inline-flex overflow-hidden rounded-lg border border-slate-200 print:hidden">
  {% for g in granularities %}
  <a href="/reports/sales?granularity={{ g }}"
     class="px-3.5 py-1.5 text-sm font-medium {{ 'bg-slate-900 text-white' if g == view.granularity else 'text-slate-600 hover:bg-slate-100' }}">
    {{ g | capitalize }}
  </a>
  {% endfor %}
</div>

{# Summary cards #}
<section class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Total Revenue</div>
    <div class="mt-1 flex items-baseline gap-2">
      <span class="text-lg font-semibold text-slate-900">{{ view.total_revenue | money }}</span>
      {% if view.revenue_delta %}{{ ui.delta_chip(view.revenue_delta, polarity="higher_better") }}{% endif %}
    </div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Units</div>
    <div class="mt-1 flex items-baseline gap-2">
      <span class="text-lg font-semibold text-slate-900">{{ view.total_units }}</span>
      {% if view.units_delta %}{{ ui.delta_chip(view.units_delta, polarity="higher_better") }}{% endif %}
    </div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Orders</div>
    <div class="mt-1 text-lg font-semibold text-slate-900">{{ view.total_orders }}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Avg AOV</div>
    <div class="mt-1 text-lg font-semibold text-slate-900">{{ view.avg_aov | money }}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Avg Daily Revenue</div>
    <div class="mt-1 text-lg font-semibold text-slate-900">{{ view.avg_daily_revenue | money }}</div>
    {% if view.peak %}<div class="mt-0.5 text-[11px] text-slate-500">Peak {{ view.peak.label }}: {{ view.peak.revenue | money }}</div>{% endif %}
  </div>
</section>

{# CHART_PLACEHOLDER — bar chart inserted in Task 3 #}

{# Velocity table #}
<div class="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
  <table class="min-w-full text-sm">
    <thead>
      <tr class="border-b border-slate-200 text-left text-[11px] uppercase tracking-wider text-slate-500">
        <th class="px-4 py-2 font-semibold">Period</th>
        <th class="px-4 py-2 text-right font-semibold">Revenue</th>
        <th class="px-4 py-2 text-right font-semibold">Units</th>
        <th class="px-4 py-2 text-right font-semibold">Orders</th>
        <th class="px-4 py-2 text-right font-semibold">AOV</th>
      </tr>
    </thead>
    <tbody>
      {% for b in view.buckets %}
      <tr class="border-b border-slate-100 {{ 'bg-slate-50 text-slate-400' if b.in_progress }}">
        <td class="px-4 py-2 font-medium text-slate-700">
          {{ b.label }}
          {% if b.in_progress %}<span class="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-slate-400">in progress</span>{% endif %}
        </td>
        <td class="px-4 py-2 text-right tabular-nums text-slate-900">{{ b.revenue | money }}</td>
        <td class="px-4 py-2 text-right tabular-nums text-slate-700">{{ b.units }}</td>
        <td class="px-4 py-2 text-right tabular-nums text-slate-700">{{ b.orders }}</td>
        <td class="px-4 py-2 text-right tabular-nums text-slate-700">{{ b.aov | money }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

- [ ] **Step 5: Run the route tests**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -20`
Expected: 4 passed.

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: /reports/sales page (cards, toggle, velocity table)

Add the GET route + reports/sales.html with summary cards (totals, AOV,
avg-daily velocity, trend deltas, peak), a daily/weekly/monthly toggle, and
the per-bucket velocity table. Download-CSV button points at the route added
next.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py app/templates/reports/sales.html tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: Inline-SVG revenue bar chart

**Files:**
- Modify: `app/routers/reports.py` (add `chart` to the context)
- Modify: `app/templates/reports/sales.html` (replace the placeholder)
- Test: `tests/test_sales_page.py` (assert the chart renders)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_sales_page.py`:
```python
def test_sales_page_has_revenue_chart(client):
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 1)
        db.commit()
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text     # chart section heading
    assert "<svg" in r.text                  # inline-SVG bar chart rendered
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py::test_sales_page_has_revenue_chart -v 2>&1 | tail -12`
Expected: FAIL — "Revenue velocity" not in the page yet.

- [ ] **Step 3: Build the chart in the route**

In `app/routers/reports.py`, `bar_chart` is already imported (from `app.reports.dashboard_trends`). Update `sales_view` to compute and pass the chart:
```python
@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily", db: Session = Depends(get_db)):
    """Sales velocity — revenue/units/orders per day/week/month with trend."""
    view = compute_sales_report(db, granularity)
    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    chart = bar_chart([float(b.revenue) for b in view.buckets])
    return templates.TemplateResponse(
        request, "reports/sales.html",
        {"view": view, "granularities": GRANULARITIES,
         "window_label": window_label, "chart": chart},
    )
```

- [ ] **Step 4: Replace the placeholder in the template**

In `app/templates/reports/sales.html`, replace the line `{# CHART_PLACEHOLDER — bar chart inserted in Task 3 #}` with:
```html
{# Revenue velocity — zero-dep inline-SVG bar chart (reuses ui.barchart). #}
{% if chart %}
{% set tips = namespace(t=[]) %}
{% for b in view.buckets %}
  {% set tips.t = tips.t + [b.label ~ ": " ~ (b.revenue | money)] %}
{% endfor %}
<section aria-label="Revenue velocity" class="mb-5 rounded-xl border border-slate-200 bg-white p-4 shadow-sm print:hidden">
  <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Revenue velocity</div>
  <div class="mt-2">{{ ui.barchart(chart, tooltips=tips.t, pos_tone="info", neg_tone="neg") }}</div>
</section>
{% endif %}
```

- [ ] **Step 5: Run tests**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -20`
Expected: 5 passed.

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: inline-SVG revenue velocity bar chart

Reuse dashboard_trends.bar_chart + the ui.barchart macro to draw revenue per
bucket above the table, matching the P&L trend-chart style. Tooltips built
from the same buckets.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py app/templates/reports/sales.html tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: `/reports/sales.csv` export

**Files:**
- Modify: `app/routers/reports.py`
- Test: `tests/test_sales_page.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_sales_page.py`:
```python
def test_sales_csv_exports_velocity_table(client):
    with SessionLocal() as db:
        _seed(db, date.today(), 100, 2)
        db.commit()
    r = client.get("/reports/sales.csv?granularity=daily")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"]
    assert len(rows) >= 2          # header + at least one bucket
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py::test_sales_csv_exports_velocity_table -v 2>&1 | tail -12`
Expected: FAIL — 404.

- [ ] **Step 3: Add the CSV route**

In `app/routers/reports.py`, add next to the other `*.csv` routes (e.g. after `ad_spend_daily_csv`). Reuses the existing `_csv_response` helper:
```python
@router.get("/reports/sales.csv")
def sales_csv(granularity: str = "daily", db: Session = Depends(get_db)) -> Response:
    """Sales velocity table as CSV (mirrors the /reports/sales table)."""
    view = compute_sales_report(db, granularity)

    def rows():
        for b in view.buckets:
            yield [
                b.label, b.start.isoformat(), f"{b.revenue:.2f}",
                b.units, b.orders, f"{b.aov:.2f}",
                "yes" if b.in_progress else "",
            ]

    return _csv_response(
        rows(),
        ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"],
        f"sales_{view.granularity}.csv",
    )
```

- [ ] **Step 4: Run tests**

Run: `py -m pytest tests/test_sales_page.py -v 2>&1 | tail -20`
Expected: 6 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: CSV export

Add GET /reports/sales.csv mirroring the velocity table (Period/Start/
Revenue/Units/Orders/AOV/In Progress) for the active granularity, via the
shared _csv_response helper.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 5: Top-level "Sales" nav link

**Files:**
- Modify: `app/templates/partials/nav.html`
- Test: `tests/test_sales_page.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_sales_page.py`:
```python
def test_nav_has_sales_link(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert 'href="/reports/sales"' in r.text     # top-level nav link present
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_page.py::test_nav_has_sales_link -v 2>&1 | tail -12`
Expected: it may already pass if the Download-CSV/toggle hrefs contain `/reports/sales` — to make this assertion meaningful, it specifically checks the bare nav href. If it passes for the wrong reason, tighten by asserting the nav list entry exists after Step 3; otherwise proceed (the nav addition is the real deliverable).

- [ ] **Step 3: Add the nav link**

In `app/templates/partials/nav.html`, the `primary_links_left` set is:
```jinja
      {% set primary_links_left = [
        ("/", "Dashboard"),
        ("/reports/pnl", "P&L"),
      ] %}
```
Change it to add Sales:
```jinja
      {% set primary_links_left = [
        ("/", "Dashboard"),
        ("/reports/pnl", "P&L"),
        ("/reports/sales", "Sales"),
      ] %}
```

- [ ] **Step 4: Run tests + a nav-render regression**

Run: `py -m pytest tests/test_sales_page.py -q 2>&1 | tail -6; py -m pytest -k "nav or dashboard or pnl" -q 2>&1 | tail -8`
Expected: all pass (nav still parses; the new link renders on every page).

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sales report: top-level Sales nav link

Add a Sales link to primary_links_left, between P&L and Action Center.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/partials/nav.html tests/test_sales_page.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 6: Full suite + deploy

**Files:** none (verification + ship)

- [ ] **Step 1: Full test suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 809 passed + the new sales tests; 11 skipped).

- [ ] **Step 2: Local visual smoke (optional but recommended)**

Boot uvicorn locally per the repo pattern and load `/reports/sales`, toggling granularities, to confirm the chart + table render (tests prove parse/route, not visual layout). This is for the implementer's confidence; the user will do the authoritative eyeball pass on prod.

- [ ] **Step 3: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/sales-velocity-report
git checkout main && git pull --ff-only
git merge --no-ff feature/sales-velocity-report -m "Merge feature/sales-velocity-report"
git push origin main
git branch -d feature/sales-velocity-report && git push origin --delete feature/sales-velocity-report
fly deploy
```
This deploy also carries the already-committed `tiktok_sync` docstring fix sitting on `main`. No schema change → the release `alembic upgrade head` is a no-op.

- [ ] **Step 4: Verify on prod**

`fly releases` shows the new release healthy; load `https://smashbox.fly.dev/reports/sales`, confirm the page renders with the chart + table, the toggle works, and Download CSV returns a file. Confirm via `fly releases` + page load, NOT a machine restart. Then ask the user for the authoritative eyeball pass.

---

## Self-Review

**Spec coverage:**
- Reuse `compute_sales_report` + remove dead helpers → Task 1. ✓
- `/reports/sales` route (all-users), granularity default/fallback → Task 2. ✓
- Page: header/window, toggle, summary cards (totals, AOV, avg-daily, deltas, peak), velocity table, in-progress flag → Task 2. ✓
- Inline-SVG bar chart via `ui.barchart` / `bar_chart` → Task 3. ✓
- `/reports/sales.csv` mirroring the table via `_csv_response` → Task 4. ✓
- Top-level "Sales" nav link → Task 5. ✓
- Tests: bucketing, GMV formula, delta-excludes-in-progress, peak, empty, **MonthlyPnL.gmv parity**, route, CSV, nav → Tasks 1–5. ✓
- Out-of-scope items (fiscal, drilldown, xlsx, custom range) honored — none added. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The template's `{# CHART_PLACEHOLDER #}` is an intentional, explicitly-replaced marker (Task 3 Step 4), not a left-behind stub.

**Type consistency:** `compute_sales_report(db, granularity, *, as_of=None)` and `SalesReportView`/`SalesBucket` attributes (`buckets`, `total_revenue`, `total_units`, `total_orders`, `avg_aov`, `avg_daily_revenue`, `revenue_delta`, `units_delta`, `peak`, `window_start`, `window_end`, `granularity`; bucket `label`/`key`/`start`/`revenue`/`units`/`orders`/`aov`/`in_progress`) match the module and are used identically across the route, template, CSV, and tests. `bar_chart(list[float]) -> BarChart` and `ui.barchart(chart, tooltips, pos_tone, neg_tone)` match `dashboard_trends` + `ui.html`. `_csv_response(rows, header, filename)` matches the helper signature. ✓
