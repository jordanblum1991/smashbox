# SKU Performance Report (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a **SKUs** tab to `/reports/sales` with a per-SKU performance table (units / net sales / orders / % / momentum vs prior period / 6-status lifecycle / sparkline), an insights strip, and an inactive-catalog toggle — over the report's existing period scopes.

**Architecture:** A new pure-computation module `app/reports/sku_performance.py` (two-window per-SKU aggregation + classification + insights), the existing `sales_view` route extended with `tab`/`sort`/`show_inactive`, and `sales.html` gaining an Overview/SKUs/Timing tab bar (Overview = today's content, unchanged).

**Tech Stack:** SQLAlchemy 2.x, Jinja2 + `partials/ui.html` (`delta_chip`/`sparkline`), pytest. Spec: `docs/superpowers/specs/2026-06-22-sku-performance-design.md`.

**Branch:** `feature/sku-performance` (created; spec committed).

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25` (NOT PowerShell/venv).
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — a literal `%` breaks printf), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Money is `Decimal`. Starlette: `templates.TemplateResponse(request, "x.html", {...})`.

---

## File Structure

- **Create** `app/reports/sku_performance.py` — the compute module (Task 1).
- **Modify** `app/routers/reports.py` — `sales_view` gains `tab`/`sort`/`show_inactive` (Task 2).
- **Modify** `app/templates/reports/sales.html` — tab bar + wrap Overview + SKUs tab (Task 3).
- **Tests:** `tests/test_sku_performance.py` (compute), `tests/test_sales_skus_tab.py` (route + template).

---

## Task 1: `compute_sku_performance` module

**Files:**
- Create: `app/reports/sku_performance.py`
- Test: `tests/test_sku_performance.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sku_performance.py
"""Per-SKU sales performance: two-window aggregation, momentum, the 6-status
lifecycle, insights, inactive catalog. Seeds PAID orders + lines; no network."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.sku_performance import compute_sku_performance

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, tiktok_id, code, name):
    db.add(Sku(sku=code, name=name, brand="smashbox", tiktok_sku_id=tiktok_id,
               unit_cogs=Decimal("0")))
    db.flush()


def _order(db, d: date, sku_id, qty, *, gross=None, order_type=OrderType.PAID,
           platform_discount=0, outlandish=0, smashbox=0):
    """One PAID order on day d (noon) with a single line for sku_id."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross if gross is not None else qty * 10)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku_id, quantity=qty,
                     gross_sales=Decimal(str(gross if gross is not None else qty * 10)),
                     platform_discount=Decimal(str(platform_discount)),
                     seller_funded_outlandish=Decimal(str(outlandish)),
                     seller_funded_smashbox=Decimal(str(smashbox))))
    db.flush()


# Window helper: selected = May 16–31 (16 days), prior = Apr 30–May 15.
SEL_START, SEL_END = date(2026, 5, 16), date(2026, 5, 31)


def test_units_net_orders_aggregation():
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        # 2 orders for S1 in window: qty 3 + 2 = 5 units, 2 orders.
        _order(db, date(2026, 5, 20), "S1", 3, gross=100, platform_discount=10,
               outlandish=5, smashbox=3)   # net = 100-10-5-3 = 82
        _order(db, date(2026, 5, 22), "S1", 2, gross=40)  # net = 40
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    row = next(r for r in v.rows if r.sku_id == "S1")
    assert row.units == 5
    assert row.orders == 2
    assert row.net_sales == Decimal("122.00")   # 82 + 40
    assert row.code == "SBX-1" and row.name == "Primer"
    assert v.total_units == 5


def test_momentum_and_status_rising_declining_steady():
    with SessionLocal() as db:
        for sk in ("UP", "DOWN", "FLAT"):
            _sku(db, sk, f"SBX-{sk}", sk)
        # prior window (Apr 30–May 15): give each a baseline of 10 units
        _order(db, date(2026, 5, 10), "UP", 10)
        _order(db, date(2026, 5, 10), "DOWN", 10)
        _order(db, date(2026, 5, 10), "FLAT", 10)
        # selected window (May 16–31): UP=20 (+100%), DOWN=4 (-60%), FLAT=11 (+10%)
        _order(db, date(2026, 5, 20), "UP", 20)
        _order(db, date(2026, 5, 20), "DOWN", 4)
        _order(db, date(2026, 5, 20), "FLAT", 11)
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    by = {r.sku_id: r for r in v.rows}
    assert by["UP"].status == "rising" and by["UP"].momentum.state == "up"
    assert by["DOWN"].status == "declining" and by["DOWN"].momentum.state == "down"
    assert by["FLAT"].status == "steady"
    assert by["UP"].prior_units == 10


def test_new_stalled_inactive_statuses():
    with SessionLocal() as db:
        _sku(db, "NEW", "SBX-N", "New")
        _sku(db, "STALL", "SBX-S", "Stall")
        _sku(db, "DEAD", "SBX-D", "Dead")   # catalog, never sold → inactive
        _order(db, date(2026, 5, 20), "NEW", 5)            # first-ever sale, in window
        _order(db, date(2026, 5, 5), "STALL", 8)           # sold only in prior window
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    by = {r.sku_id: r for r in v.rows}
    assert by["NEW"].status == "new"
    assert by["STALL"].status == "stalled" and by["STALL"].units == 0
    assert v.insights.new_count == 1
    assert v.insights.stalled_count == 1
    # DEAD is inactive (catalog, no sales either window) — not in rows, but counted.
    assert "DEAD" not in by
    assert v.inactive_count == 1
    assert any(r.sku_id == "DEAD" for r in v.inactive_rows)


def test_unmapped_sku_and_paid_only_and_sparkline():
    with SessionLocal() as db:
        # No Sku row for "RAW" → Unmapped.
        _order(db, date(2026, 5, 20), "RAW", 4)
        _order(db, date(2026, 5, 21), "RAW", 2)
        # A SAMPLE order must be excluded.
        _order(db, date(2026, 5, 22), "RAW", 99, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END)
    raw = next(r for r in v.rows if r.sku_id == "RAW")
    assert raw.units == 6                 # 4 + 2; the 99 SAMPLE excluded
    assert raw.code == "Unmapped"
    assert raw.spark != ""                # two days of data → a drawable sparkline


def test_insights_and_sort():
    with SessionLocal() as db:
        for sk in ("A", "B", "C"):
            _sku(db, sk, f"SBX-{sk}", sk)
        _order(db, date(2026, 5, 10), "A", 10)   # prior
        _order(db, date(2026, 5, 10), "B", 10)
        _order(db, date(2026, 5, 20), "A", 30)   # +200% riser, 30 units (top seller)
        _order(db, date(2026, 5, 20), "B", 2)    # -80% faller
        _order(db, date(2026, 5, 20), "C", 5)    # new
        db.commit()
        v = compute_sku_performance(db, start=SEL_START, end=SEL_END, sort="units")
    assert v.insights.top_seller.sku_id == "A"
    assert v.insights.biggest_riser.sku_id == "A"
    assert v.insights.biggest_faller.sku_id == "B"
    assert [r.sku_id for r in v.rows][0] == "A"   # sorted by units desc
    v2 = compute_sku_performance(db, start=SEL_START, end=SEL_END, sort="orders")
    assert v2.rows  # sort param accepted
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sku_performance.py -v 2>&1 | tail -25`
Expected: FAIL — `No module named 'app.reports.sku_performance'`.

- [ ] **Step 3: Create the module**

```python
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
from app.models.sku import Sku
from app.reports.dashboard_trends import Delta, compute_delta, sparkline_points
from app.services.reporting_tz import placed_local_date, placed_window

_CENTS = Decimal("0.01")
RISING_PCT = Decimal("25")               # ±25% momentum band
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
                            sort: str = "units", as_of: date | None = None) -> SkuPerformanceView:
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

    # Catalog name/code map (canonical TikTok SKU ID → code/name).
    catalog = {s.tiktok_sku_id: (s.sku, s.name)
               for s in db.execute(select(Sku).where(Sku.tiktok_sku_id.isnot(None))).scalars()}

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
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_sku_performance.py -v 2>&1 | tail -25`
Expected: 5 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
sku performance: compute module (two-window per-SKU analytics)

compute_sku_performance aggregates PAID per-SKU units/net-sales/orders for the
selected window + prior equal window, classifies the 6-status lifecycle
(New/Rising/Steady/Declining/Stalled/Inactive at +-25%), builds momentum +
sparkline + insights, and lists inactive catalog SKUs. Pure computation.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/sku_performance.py tests/test_sku_performance.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: Route — `tab` / `sort` / `show_inactive`

**Files:**
- Modify: `app/routers/reports.py` (`sales_view`)
- Test: `tests/test_sales_skus_tab.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sales_skus_tab.py
"""The SKUs tab on /reports/sales: route wires compute_sku_performance + the
template renders the table/insights; Overview stays the default."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db):
    db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox", tiktok_sku_id="S1",
               unit_cogs=Decimal("0")))
    db.flush()
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime.now().replace(hour=12, minute=0, second=0, microsecond=0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal("100"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=5, gross_sales=Decimal("100")))
    db.flush()


def test_default_tab_is_overview(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text          # Overview content present


def test_skus_tab_renders_table_and_insights(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "SBX-1" in r.text                       # the SKU code in the table
    assert "Primer" in r.text                       # the SKU name
    assert "Top seller" in r.text or "Top:" in r.text   # insights strip


def test_skus_tab_sort_and_inactive_params_accepted(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    assert client.get("/reports/sales?tab=skus&sort=net_sales").status_code == 200
    assert client.get("/reports/sales?tab=skus&show_inactive=1").status_code == 200
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sales_skus_tab.py -v 2>&1 | tail -15`
Expected: `test_skus_tab_renders_table_and_insights` FAILS (no SKU table yet); the others may pass trivially (page renders).

- [ ] **Step 3: Extend `sales_view`**

In `app/routers/reports.py`, replace the `sales_view` function with:
```python
@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily",
               start_date: str | None = None, end_date: str | None = None,
               year: int | None = None, month: int | None = None,
               tab: str = "overview", sort: str = "units", show_inactive: int = 0,
               db: Session = Depends(get_db)):
    """Sales report — Overview (velocity) or SKUs (per-SKU performance) tab, over
    the calendar/custom-range/fiscal period scopes."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    ctx["tab"] = "skus" if tab == "skus" else "overview"
    ctx["sort"] = sort
    ctx["show_inactive"] = bool(show_inactive)
    if ctx["tab"] == "skus":
        from app.reports.sku_performance import compute_sku_performance
        v = ctx["view"]
        ctx["sku"] = compute_sku_performance(db, start=v.window_start, end=v.window_end, sort=sort)
    return templates.TemplateResponse(request, "reports/sales.html", ctx)
```
(The CSV route is unchanged.)

- [ ] **Step 4: Run the route tests**

Run: `py -m pytest tests/test_sales_skus_tab.py -v 2>&1 | tail -15`
Expected: `test_default_tab_is_overview` + the param tests pass; `test_skus_tab_renders_table_and_insights` still FAILS (template not done — Task 3). Leave it failing; it passes after Task 3. (Do NOT delete it.)

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
sku performance: route tab/sort/show_inactive

sales_view gains tab (overview|skus) + sort + show_inactive; when tab=skus it
computes compute_sku_performance over the resolved window and passes it to the
template. Overview is the default; period scopes unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py tests/test_sales_skus_tab.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: Template — tab bar + SKUs tab

**Files:**
- Modify: `app/templates/reports/sales.html`
- Test: `tests/test_sales_skus_tab.py`

READ `app/templates/reports/sales.html` first. The period controls end at the `{% endif %}` that closes the fiscal/range form block (around line 69); below that are the summary cards + chart + velocity table, ending at `{% endblock %}`.

- [ ] **Step 1: Define a reusable period query-string + the tab bar**

Near the top of `{% block content %}` (after the `{% import %}` lines / before or right after the `page_header`), add a `period_qs` set so all tab/sort links carry the active period:
```html
{% set period_qs = "granularity=" ~ granularity
   ~ (("&start_date=" ~ start_date ~ "&end_date=" ~ end_date) if (start_date and end_date) else "")
   ~ (("&year=" ~ fiscal_year ~ "&month=" ~ fiscal_month) if fiscal_year else "") %}
```

After the period-controls block (right after the `{% endif %}` near line 69, before the summary cards), add the tab bar:
```html
{# Report tabs — Overview (velocity) / SKUs (per-SKU performance) / Timing (soon). #}
<div class="mb-5 flex items-center gap-1 border-b border-slate-200 text-sm print:hidden">
  <a href="/reports/sales?{{ period_qs }}&tab=overview"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab != 'skus' else 'border-transparent text-slate-500 hover:text-slate-800' }}">Overview</a>
  <a href="/reports/sales?{{ period_qs }}&tab=skus"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab == 'skus' else 'border-transparent text-slate-500 hover:text-slate-800' }}">SKUs</a>
  <span class="-mb-px cursor-not-allowed border-b-2 border-transparent px-3 py-2 font-medium text-slate-300" title="Coming soon">Timing
    <span class="ml-1 rounded bg-slate-100 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-slate-400">soon</span>
  </span>
</div>
```

- [ ] **Step 2: Wrap the Overview content + add the SKUs tab**

Wrap ALL the existing post-tab content (the summary cards section through the velocity table — everything from the `{# Summary cards #}` comment down to just before `{% endblock %}`) in `{% if tab != 'skus' %}` … `{% endif %}`. Then, immediately before `{% endblock %}`, add the SKUs tab:

```html
{% if tab == 'skus' %}
{# ── SKUs tab — per-SKU performance ───────────────────────────────────── #}
{% set ins = sku.insights %}
<section aria-label="SKU insights" class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">🏆 Top seller</div>
    <div class="mt-1 truncate text-sm font-semibold text-slate-900">{% if ins.top_seller %}{{ ins.top_seller.code }}{% else %}—{% endif %}</div>
    <div class="text-[11px] text-slate-500">{% if ins.top_seller %}{{ ins.top_seller.units }} units{% endif %}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">📈 Biggest riser</div>
    <div class="mt-1 truncate text-sm font-semibold text-slate-900">{% if ins.biggest_riser %}{{ ins.biggest_riser.code }}{% else %}—{% endif %}</div>
    <div class="text-[11px] text-emerald-600">{% if ins.biggest_riser and ins.biggest_riser.momentum %}{{ ins.biggest_riser.momentum.label }}{% endif %}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">📉 Biggest faller</div>
    <div class="mt-1 truncate text-sm font-semibold text-slate-900">{% if ins.biggest_faller %}{{ ins.biggest_faller.code }}{% else %}—{% endif %}</div>
    <div class="text-[11px] text-rose-600">{% if ins.biggest_faller and ins.biggest_faller.momentum %}{{ ins.biggest_faller.momentum.label }}{% endif %}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">🆕 New</div>
    <div class="mt-1 text-lg font-semibold text-slate-900">{{ ins.new_count }}</div>
  </div>
  <div class="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">⏸️ Stalled</div>
    <div class="mt-1 text-lg font-semibold text-slate-900">{{ ins.stalled_count }}</div>
  </div>
</section>

{% set badge = {
  'new':'bg-sky-50 text-sky-700', 'rising':'bg-emerald-50 text-emerald-700',
  'steady':'bg-slate-100 text-slate-600', 'declining':'bg-rose-50 text-rose-700',
  'stalled':'bg-amber-50 text-amber-700', 'inactive':'bg-slate-100 text-slate-400' } %}
{% macro sort_th(label, key, align='right') %}
  <th class="px-3 py-2 text-{{ align }} font-semibold">
    <a href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ key }}{% if show_inactive %}&show_inactive=1{% endif %}"
       class="hover:text-slate-900 {{ 'text-slate-900 underline' if sort == key else '' }}">{{ label }}</a>
  </th>
{% endmacro %}

<div class="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
  <table class="min-w-full text-sm">
    <thead>
      <tr class="border-b border-slate-200 text-left text-[11px] uppercase tracking-wider text-slate-500">
        <th class="px-3 py-2 font-semibold">SKU</th>
        {{ sort_th("Units", "units") }}
        {{ sort_th("Net Sales", "net_sales") }}
        {{ sort_th("Orders", "orders") }}
        <th class="px-3 py-2 text-right font-semibold">%</th>
        {{ sort_th("Momentum", "momentum") }}
        <th class="px-3 py-2 text-right font-semibold">Trend</th>
        <th class="px-3 py-2 text-right font-semibold">Status</th>
      </tr>
    </thead>
    <tbody>
      {% for r in sku.rows %}
      <tr class="border-b border-slate-100">
        <td class="px-3 py-2"><div class="font-medium text-slate-800">{{ r.code }}</div><div class="truncate text-[11px] text-slate-500">{{ r.name }}</div></td>
        <td class="px-3 py-2 text-right tabular-nums font-medium text-slate-900">{{ r.units }}</td>
        <td class="px-3 py-2 text-right tabular-nums text-slate-700">{{ r.net_sales | money }}</td>
        <td class="px-3 py-2 text-right tabular-nums text-slate-700">{{ r.orders }}</td>
        <td class="px-3 py-2 text-right tabular-nums text-slate-500">{{ r.pct_units }}%</td>
        <td class="px-3 py-2 text-right">{% if r.momentum %}{{ ui.delta_chip(r.momentum, polarity="higher_better") }}{% endif %}</td>
        <td class="px-3 py-2 text-right">{{ ui.sparkline(r.spark, tone="info") }}</td>
        <td class="px-3 py-2 text-right"><span class="rounded px-1.5 py-0.5 text-[10px] font-semibold capitalize {{ badge[r.status] }}">{{ r.status }}</span></td>
      </tr>
      {% else %}
      <tr><td colspan="8" class="px-3 py-6 text-center text-slate-400">No SKU sales in this period.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

{# Inactive catalog SKUs — count always; rows behind a toggle. #}
{% if sku.inactive_count %}
<div class="mt-3 text-sm print:hidden">
  {% if show_inactive %}
  <a href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ sort }}" class="text-slate-500 hover:text-slate-700">💤 Hide {{ sku.inactive_count }} inactive SKUs ▴</a>
  <div class="mt-2 overflow-x-auto rounded-xl border border-slate-200 bg-slate-50/50">
    <table class="min-w-full text-sm">
      <tbody>
        {% for r in sku.inactive_rows %}
        <tr class="border-b border-slate-100"><td class="px-3 py-1.5 text-slate-500">{{ r.code }}</td><td class="px-3 py-1.5 truncate text-[11px] text-slate-400">{{ r.name }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <a href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ sort }}&show_inactive=1" class="text-slate-500 hover:text-slate-700">💤 Show {{ sku.inactive_count }} inactive SKUs ▾</a>
  {% endif %}
</div>
{% endif %}
{% endif %}
```

- [ ] **Step 3: Preserve the active tab when changing scope**

So switching granularity/fiscal/range while on the SKUs tab stays on SKUs: append `&tab={{ tab }}` to the existing scope links. In the granularity-toggle loop link (`/reports/sales?granularity={{ g }}...`), the Fiscal dropdown links, and the Clear link, add `&tab={{ tab }}` at the end of each href. (The `&tab=overview` default is harmless.) Leave the CSV link as-is (Overview-only export in Phase 1).

- [ ] **Step 4: Run the SKUs-tab tests + render regression**

Run: `py -m pytest tests/test_sales_skus_tab.py -v 2>&1 | tail -15`
Expected: all pass (incl. `test_skus_tab_renders_table_and_insights` now).
Run: `py -m pytest tests/test_sales_page.py -q 2>&1 | tail -8`
Expected: pass (Overview + period scopes unchanged).

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
sku performance: Overview/SKUs/Timing tabs + SKU table UI

Add the tab bar; wrap today's velocity view as the Overview tab; build the SKUs
tab — insights strip, sortable per-SKU table (units/net sales/orders/%/momentum
chip/sparkline/status badge), and an inactive-catalog toggle. Scope links carry
the active tab.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/reports/sales.html tests/test_sales_skus_tab.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: Full suite + deploy + verify

**Files:** none (verification + ship)

- [ ] **Step 1: Full suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 861 + the new SKU tests; 11 skipped).

- [ ] **Step 2: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/sku-performance
git checkout main && git pull --ff-only
git merge --no-ff feature/sku-performance -m "Merge feature/sku-performance"
git push origin main
git branch -d feature/sku-performance && git push origin --delete feature/sku-performance
fly deploy
```
No schema change → the release `alembic upgrade head` is a no-op.

- [ ] **Step 3: Verify on prod**

`fly releases` healthy; load `https://smashbox.fly.dev/reports/sales?tab=skus` — confirm the SKU table renders with real catalog data, the insights strip populates, sort header links work, and the inactive toggle reveals rows. Confirm `?tab=overview` (default) is unchanged. Then ask the user for an eyeball pass (desktop + phone — the table scrolls horizontally on mobile per the responsive pass).

---

## Self-Review

**Spec coverage:**
- Per-SKU units / net sales / orders / % over the selected window → Task 1 (`compute_sku_performance`). ✓
- Momentum vs prior equal window + 6-status lifecycle (±25%) → Task 1 (`compute_delta`, `_classify`). ✓
- Per-SKU sparkline → Task 1 (`sparkline_points`). ✓
- Insights strip (top seller / riser / faller / new / stalled) → Task 1 (`SkuInsights`) + Task 3 (strip). ✓
- Inactive catalog count + toggle → Task 1 (`inactive_rows/count`) + Task 3 (toggle). ✓
- Tabs (Overview/SKUs/Timing) → Task 3; Overview unchanged → Task 3 wrap. ✓
- Route tab/sort/show_inactive; window from existing scopes → Task 2. ✓
- Net Sales = line gross − platform − outlandish − smashbox → Task 1 (`_NET`). ✓
- Default Units-desc, sortable → Task 1 (`_sort_value`) + Task 3 (sort_th). ✓
- PAID-only, unmapped→"Unmapped", empty window → Task 1 + tests. ✓
- Out-of-scope (timing/heatmap/CSV/profit) honored. ✓

**Placeholder scan:** No TBD/TODO; full module + template markup provided. The Task 2 note that one test stays red until Task 3 is explicit (not a gap).

**Type consistency:** `compute_sku_performance(db, *, start, end, sort="units", as_of=None) -> SkuPerformanceView`; `SkuPerfRow`/`SkuInsights`/`SkuPerformanceView` field names used identically across Task 1 (module), the route's `ctx["sku"]`, and Task 3's template (`sku.rows`, `sku.insights.top_seller.code`, `r.momentum`, `r.spark`, `r.status`, `sku.inactive_count/inactive_rows`). The `delta_chip(delta, polarity)` + `sparkline(points, tone)` macro calls match `ui.html`. `period_qs`/`tab`/`sort`/`show_inactive` context keys match between Task 2 and Task 3. ✓
