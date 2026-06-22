# SKU Table Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 10/25/50/100 page-size selector (default 25) + Prev/numbers/Next pagination to the active SKU table on `/reports/sales?tab=skus`.

**Architecture:** Server-side, query-param driven. The route slices the already-sorted `sku.rows` for the current page and passes pager metadata; the template renders a size selector + pager. Insights / "%" / totals stay over the full set. Spec: `docs/superpowers/specs/2026-06-22-sku-table-pagination-design.md`.

**Tech Stack:** FastAPI, Jinja2 + compiled Tailwind, pytest. Branch: `feature/sku-table-pagination`.

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25`.
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — `%` breaks it), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Task 1: Route + template pagination

**Files:**
- Modify: `app/routers/reports.py` (`sales_view` + a new module constant)
- Modify: `app/templates/reports/sales.html` (SKUs tab)
- Test: `tests/test_sku_pagination.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sku_pagination.py`:

```python
# tests/test_sku_pagination.py
"""Pagination on the SKUs tab: 10/25/50/100 size selector (default 25) + pager.
Seeds 30 PAID SKUs (SKU i gets i units → deterministic units-desc order:
SBX-030 top … SBX-001 last). Names are distinct from codes so table-name checks
aren't polluted by the insights strip (which renders codes only)."""
import itertools
from datetime import datetime
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


def _seed_n(db, n):
    """n SKUs; SKU i (1..n) gets i units in one PAID order placed now."""
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    for i in range(1, n + 1):
        code = f"SBX-{i:03d}"
        db.add(Sku(sku=code, name=f"ProductName{i:03d}", brand="smashbox",
                   tiktok_sku_id=f"T{i:03d}", unit_cogs=Decimal("0")))
        db.flush()
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="t", stored_path="t")
        db.add(b); db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=now,
                  order_type=OrderType.PAID, status="Completed", brand="smashbox",
                  gross_sales=Decimal(str(i * 10)))
        db.add(o); db.flush()
        db.add(OrderLine(order_id=o.id, sku=f"T{i:03d}", quantity=i,
                         gross_sales=Decimal(str(i * 10))))
        db.flush()


# Units-desc: SBX-030 (30u) … SBX-006 is the 25th, SBX-005 the 26th, SBX-001 last.
# Negative assertions avoid SBX-030 (the insights strip always renders the top seller).

def test_default_page_size_25_first_page(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "of 30" in r.text              # total count shown
    assert "SBX-030" in r.text            # top seller on page 1
    assert "SBX-005" not in r.text        # 26th — belongs to page 2
    assert "SBX-001" not in r.text        # last — page 2


def test_second_page_shows_remainder(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=25&page=2")
    assert r.status_code == 200
    assert "SBX-001" in r.text            # remainder on page 2
    assert "SBX-006" not in r.text        # 25th — was on page 1


def test_invalid_per_page_falls_back_to_25(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=7")
    assert r.status_code == 200
    assert "SBX-006" in r.text            # the 25th row present → size is 25, not 7
    assert "SBX-005" not in r.text


def test_page_out_of_range_clamps_to_last(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=25&page=99")
    assert r.status_code == 200
    assert "SBX-001" in r.text            # clamped to the last page (page 2)


def test_per_page_100_shows_all(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=100")
    assert r.status_code == 200
    assert "SBX-001" in r.text and "SBX-006" in r.text   # all 30 on one page


def test_overview_unaffected(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sku_pagination.py -v 2>&1 | tail -25`
Expected: the pagination tests FAIL (e.g. `test_default_page_size_25_first_page` — currently all 30 rows render, so `SBX-001`/`SBX-005` ARE in the text). `test_overview_unaffected` passes.

- [ ] **Step 3: Extend the route**

In `app/routers/reports.py`, add the constant directly above the `@router.get("/reports/sales")` decorator:
```python
PER_PAGE_OPTIONS = (10, 25, 50, 100)
```

Replace the `sales_view` function with:
```python
@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily",
               start_date: str | None = None, end_date: str | None = None,
               year: int | None = None, month: int | None = None,
               tab: str = "overview", sort: str = "units", show_inactive: int = 0,
               per_page: int = 25, page: int = 1,
               db: Session = Depends(get_db)):
    """Sales report — Overview (velocity) or SKUs (per-SKU performance) tab, over
    the calendar/custom-range/fiscal period scopes. The SKU table is paginated."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    ctx["tab"] = "skus" if tab == "skus" else "overview"
    ctx["sort"] = sort
    ctx["show_inactive"] = bool(show_inactive)
    if ctx["tab"] == "skus":
        from app.reports.sku_performance import compute_sku_performance
        v = ctx["view"]
        sku = compute_sku_performance(db, start=v.window_start, end=v.window_end, sort=sort)
        ctx["sku"] = sku
        # Paginate the active rows (insights / % / totals stay over the full set).
        pp = per_page if per_page in PER_PAGE_OPTIONS else 25
        total = len(sku.rows)
        total_pages = max(1, -(-total // pp))         # ceil division
        pg = min(max(page, 1), total_pages)
        start_i = (pg - 1) * pp
        ctx["page_rows"] = sku.rows[start_i:start_i + pp]
        ctx["per_page"] = pp
        ctx["page"] = pg
        ctx["total_rows"] = total
        ctx["total_pages"] = total_pages
        ctx["per_page_options"] = PER_PAGE_OPTIONS
        ctx["row_start"] = start_i + 1 if total else 0
        ctx["row_end"] = min(start_i + pp, total)
        # Windowed page numbers (<=7), centered on the current page.
        if total_pages <= 7:
            ctx["page_window"] = list(range(1, total_pages + 1))
        else:
            lo = max(1, pg - 3)
            hi = min(total_pages, lo + 6)
            lo = max(1, hi - 6)
            ctx["page_window"] = list(range(lo, hi + 1))
    return templates.TemplateResponse(request, "reports/sales.html", ctx)
```

- [ ] **Step 4: Update the template**

In `app/templates/reports/sales.html`, inside the `{% if tab == 'skus' %}` block:

**(a)** Right after `{% set ins = sku.insights %}` (around line 176), add the shared query-string:
```html
{% set skus_qs = period_qs ~ "&tab=skus&sort=" ~ sort ~ ("&show_inactive=1" if show_inactive else "") %}
```

**(b)** Change the `sort_th` macro's href (around line 209) to carry `per_page` (re-sorting keeps the size, resets to page 1):
```html
    <a href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ key }}&per_page={{ per_page }}{% if show_inactive %}&show_inactive=1{% endif %}"
```

**(c)** Immediately BEFORE the table wrapper `<div class="overflow-x-auto rounded-xl border ...">` (around line 214), insert the control row:
```html
<div class="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm print:hidden">
  <div class="text-slate-500">
    {% if total_rows %}Showing {{ row_start }}–{{ row_end }} of {{ total_rows }}{% else %}Showing 0 of 0{% endif %}
  </div>
  <div class="flex items-center gap-1">
    <span class="text-xs text-slate-400">Per page</span>
    {% for opt in per_page_options %}
    <a href="/reports/sales?{{ skus_qs }}&per_page={{ opt }}"
       class="rounded-md px-2 py-1 font-medium {{ 'bg-slate-900 text-white' if opt == per_page else 'text-slate-600 hover:bg-slate-100' }}">{{ opt }}</a>
    {% endfor %}
  </div>
</div>
```

**(d)** Change the table body loop (around line 229) from `{% for r in sku.rows %}` to:
```html
      {% for r in page_rows %}
```
(Leave the row markup and the `{% else %}` empty-state unchanged.)

**(e)** Immediately AFTER the table wrapper's closing `</div>` (around line 245, before the `{# Inactive catalog SKUs #}` comment), insert the pager:
```html
{% if total_pages > 1 %}
<nav aria-label="SKU pages" class="mt-3 flex flex-wrap items-center justify-center gap-1 text-sm print:hidden">
  {% if page > 1 %}
  <a href="/reports/sales?{{ skus_qs }}&per_page={{ per_page }}&page={{ page - 1 }}" class="rounded-md px-3 py-1.5 font-medium text-slate-600 hover:bg-slate-100">‹ Prev</a>
  {% else %}
  <span class="rounded-md px-3 py-1.5 font-medium text-slate-300">‹ Prev</span>
  {% endif %}
  {% if page_window[0] > 1 %}
  <a href="/reports/sales?{{ skus_qs }}&per_page={{ per_page }}&page=1" class="rounded-md px-3 py-1.5 text-slate-600 hover:bg-slate-100">1</a>
  <span class="px-1 text-slate-400">…</span>
  {% endif %}
  {% for p in page_window %}
  <a href="/reports/sales?{{ skus_qs }}&per_page={{ per_page }}&page={{ p }}"
     class="rounded-md px-3 py-1.5 font-medium {{ 'bg-slate-900 text-white' if p == page else 'text-slate-600 hover:bg-slate-100' }}">{{ p }}</a>
  {% endfor %}
  {% if page_window[-1] < total_pages %}
  <span class="px-1 text-slate-400">…</span>
  <a href="/reports/sales?{{ skus_qs }}&per_page={{ per_page }}&page={{ total_pages }}" class="rounded-md px-3 py-1.5 text-slate-600 hover:bg-slate-100">{{ total_pages }}</a>
  {% endif %}
  {% if page < total_pages %}
  <a href="/reports/sales?{{ skus_qs }}&per_page={{ per_page }}&page={{ page + 1 }}" class="rounded-md px-3 py-1.5 font-medium text-slate-600 hover:bg-slate-100">Next ›</a>
  {% else %}
  <span class="rounded-md px-3 py-1.5 font-medium text-slate-300">Next ›</span>
  {% endif %}
</nav>
{% endif %}
```

**(f)** Update the two inactive-toggle links (around lines 251 and 262) to carry `per_page` so the chosen size persists when toggling inactive:
- Hide link: `href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ sort }}&per_page={{ per_page }}"`
- Show link: `href="/reports/sales?{{ period_qs }}&tab=skus&sort={{ sort }}&per_page={{ per_page }}&show_inactive=1"`

- [ ] **Step 5: Run the tests**

Run: `py -m pytest tests/test_sku_pagination.py -v 2>&1 | tail -25`
Expected: all 6 pass.
Regression: `py -m pytest tests/test_sales_skus_tab.py tests/test_sales_page.py -q 2>&1 | tail -8`
Expected: all pass (Overview + the existing SKUs-tab tests still green — note the existing SKUs-tab test seeds 1 SKU, which is `total_pages == 1` → pager hidden, table shows the row).

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
sku performance: paginate the SKU table (10/25/50/100)

Add a server-side page-size selector (default 25) + Prev/numbers/Next pager to
the SKUs tab. The route slices the sorted rows and passes pager metadata;
insights, the % column, and totals stay over the full set. Size/pager/sort/
inactive links preserve scope + state; invalid/out-of-range params degrade safely.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py app/templates/reports/sales.html tests/test_sku_pagination.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`

---

## Task 2: Full suite + deploy + verify

**Files:** none (verification + ship)

- [ ] **Step 1: Full suite** — `py -m pytest 2>&1 | tail -12`. Expected: all pass (prior 877 + 6 new).
- [ ] **Step 2: Merge + deploy (local-merge, no PR):**
```bash
git push -u origin feature/sku-table-pagination
git checkout main && git pull --ff-only
git merge --no-ff feature/sku-table-pagination -m "Merge feature/sku-table-pagination"
git push origin main
git branch -d feature/sku-table-pagination && git push origin --delete feature/sku-table-pagination
fly deploy
```
No schema change → release `alembic upgrade head` is a no-op.
- [ ] **Step 3: Verify** — `fly releases` healthy; `curl -s -o /dev/null -w "%{http_code}" https://smashbox.fly.dev/healthz` → 200. Then ask the user for the eyeball pass (desktop + phone): size selector switches 10/25/50/100, pager walks pages, sort/scope/inactive preserved.

---

## Self-Review

**Spec coverage:** size selector 10/25/50/100 default 25 (route `PER_PAGE_OPTIONS`/`pp`, template selector) ✓; full pager Prev/numbers/Next (`page_window` + nav) ✓; server-side query params ✓; active-table-only, inactive unpaged ✓; insights/%/totals whole-set (paginate only `page_rows`, compute unchanged) ✓; invalid `per_page` → 25 ✓; out-of-range `page` clamp ✓; empty → "Showing 0 of 0", pager hidden ✓; sort/period reset to page 1 (links omit `page`) ✓; state preserved on size/sort/toggle (`skus_qs` + `per_page` threading) ✓.

**Placeholder scan:** none — full route + template + tests provided.

**Type consistency:** route context keys (`page_rows`, `per_page`, `page`, `total_rows`, `total_pages`, `per_page_options`, `row_start`, `row_end`, `page_window`) match the template references exactly. `page_rows` is `list[SkuPerfRow]` — same row shape the template already renders (`r.code/name/units/net_sales/orders/pct_units/momentum/spark/status`). `PER_PAGE_OPTIONS` is the single source for both the validity check and the selector. ✓
