# SKU × Time Heatmap (Heatmap Tab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 4th **Heatmap** tab to `/reports/sales` — a top-20 SKU × {day-of-week | daypart} grid of unit counts, color-scaled per row.

**Architecture:** A new pure-computation module `app/reports/sku_time_heatmap.py`, a `tab=="heatmap"` branch + `dim` param in `sales_view`, and a Heatmap block in `sales.html` (a styled table, no new chart primitives). Spec: `docs/superpowers/specs/2026-06-23-sku-time-heatmap-design.md`.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Jinja2 + compiled Tailwind, pytest. Branch: `feature/sku-time-heatmap`.

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25`.
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — `%` breaks it), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Starlette: `templates.TemplateResponse(request, "x.html", {...})`.

---

## Task 1: `compute_sku_time_heatmap` module

**Files:**
- Create: `app/reports/sku_time_heatmap.py`
- Test: `tests/test_sku_time_heatmap.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_sku_time_heatmap.py`:

```python
# tests/test_sku_time_heatmap.py
"""SKU × time heatmap: PAID units bucketed by shop-local weekday/daypart, per-row
leveling, top-N ranking, insights. Buckets derived via placed_local() (DST-robust)."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.sku_time_heatmap import compute_sku_time_heatmap
from app.services.reporting_tz import placed_local

_OID = itertools.count(1)
WSTART, WEND = date(2026, 5, 1), date(2026, 5, 31)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, tid, code, name):
    db.add(Sku(sku=code, name=name, brand="smashbox", tiktok_sku_id=tid, unit_cogs=Decimal("0")))
    db.flush()


def _order(db, dt, sku_id, qty, order_type=OrderType.PAID):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(qty * 10)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku_id, quantity=qty, gross_sales=Decimal(str(qty * 10))))
    db.flush()


def test_units_bucket_to_weekday():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        _order(db, dt, "S1", 7); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    assert v.columns == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    row = next(r for r in v.rows if r.sku_id == "S1")
    wd = placed_local(dt).weekday()
    assert row.cells[wd].units == 7
    assert row.cells[wd].level == 4          # its own peak
    assert row.peak_label == v.columns[wd]
    assert all(c.units == 0 and c.level == 0 for c in row.cells if c.bucket != wd)


def test_daypart_dim():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        _order(db, dt, "S1", 5); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="daypart")
    assert v.columns == ["Morning", "Afternoon", "Evening", "Night"]
    h = placed_local(dt).hour
    exp = (0 if 5 <= h < 12 else 1 if 12 <= h < 17 else 2 if 17 <= h < 22 else 3)
    row = v.rows[0]
    assert row.cells[exp].units == 5 and row.cells[exp].level == 4


def test_per_row_leveling_is_relative():
    big_day, small_day = datetime(2026, 5, 20, 12, 0), datetime(2026, 5, 22, 12, 0)
    with SessionLocal() as db:
        _sku(db, "BIG", "SBX-B", "Big"); _sku(db, "SMALL", "SBX-S", "Small")
        _order(db, big_day, "BIG", 100); _order(db, small_day, "BIG", 1)
        _order(db, big_day, "SMALL", 2)            # low volume overall
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    by = {r.sku_id: r for r in v.rows}
    wd_big, wd_small = placed_local(big_day).weekday(), placed_local(small_day).weekday()
    assert by["BIG"].cells[wd_big].level == 4          # peak
    assert by["BIG"].cells[wd_small].level >= 1        # non-zero → at least level 1
    assert by["BIG"].cells[wd_small].level < 4
    # Per-row scaling: the low-volume SKU still hits level 4 in its own peak bucket.
    assert by["SMALL"].cells[wd_big].level == 4


def test_top_n_ranking():
    with SessionLocal() as db:
        for i in range(1, 26):                          # 25 SKUs, SKU i has i units
            _sku(db, f"T{i:02d}", f"SBX-{i:02d}", f"P{i}")
            _order(db, datetime(2026, 5, 20, 12, 0), f"T{i:02d}", i)
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow", top_n=20)
    assert v.total_skus == 25
    assert v.shown == 20
    assert v.rows[0].sku_id == "T25" and v.rows[0].total_units == 25   # ranked desc
    assert all(r.sku_id != "T01" for r in v.rows)        # the 5 smallest dropped


def test_busiest_col_unmapped_paid_only_and_empty():
    with SessionLocal() as db:
        # Unmapped SKU "RAW" (no Sku row); a SAMPLE order must be excluded.
        _order(db, datetime(2026, 5, 20, 12, 0), "RAW", 4)
        _order(db, datetime(2026, 5, 21, 12, 0), "RAW", 99, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    raw = next(r for r in v.rows if r.sku_id == "RAW")
    assert raw.code == "Unmapped"
    assert raw.total_units == 4                          # SAMPLE excluded
    assert v.busiest_col == v.columns[placed_local(datetime(2026, 5, 20, 12, 0)).weekday()]

    with SessionLocal() as db:
        v2 = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    assert v2.rows == [] and v2.total_skus == 0 and v2.busiest_col is None


def test_invalid_dim_falls_back_to_dow():
    with SessionLocal() as db:
        _order(db, datetime(2026, 5, 20, 12, 0), "RAW", 1); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="bogus")
    assert v.dim == "dow" and len(v.columns) == 7
```

- [ ] **Step 2: Run to verify it fails** — `py -m pytest tests/test_sku_time_heatmap.py -v 2>&1 | tail -20`. Expected: `No module named 'app.reports.sku_time_heatmap'`.

- [ ] **Step 3: Create the module** — `app/reports/sku_time_heatmap.py`:

```python
# app/reports/sku_time_heatmap.py
"""SKU × time heatmap for the Heatmap tab of /reports/sales: PAID units per SKU
bucketed by shop-local day-of-week or daypart, ranked top-N by total units, with
per-row colour levels (each SKU shaded against its own peak bucket). Pure
computation — reads the ORM, returns dataclasses.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import floor

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.services.reporting_tz import placed_local, placed_window

DIMS = ("dow", "daypart")
HEAT_LEVELS = 5                 # 0 (none) … 4 (per-row peak)

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAYPARTS = ["Morning", "Afternoon", "Evening", "Night"]


def _daypart_bucket(hour: int) -> int:
    """Same partition as temporal_patterns: Morning 5–11, Afternoon 12–16,
    Evening 17–21, Night 22–4."""
    if 5 <= hour < 12:
        return 0
    if 12 <= hour < 17:
        return 1
    if 17 <= hour < 22:
        return 2
    return 3


@dataclass
class HeatCell:
    bucket: int
    label: str
    units: int
    level: int


@dataclass
class HeatRow:
    sku_id: str
    code: str
    name: str
    total_units: int
    cells: list[HeatCell]
    peak_label: str


@dataclass
class HeatmapView:
    columns: list[str]
    rows: list[HeatRow]
    dim: str
    total_skus: int
    shown: int
    busiest_col: str | None
    window_start: date
    window_end: date


def compute_sku_time_heatmap(db: Session, *, start: date, end: date,
                             dim: str = "dow", top_n: int = 20) -> HeatmapView:
    if dim not in DIMS:
        dim = "dow"
    columns = _WEEKDAYS if dim == "dow" else _DAYPARTS
    n_cols = len(columns)

    q_start = datetime(start.year, start.month, start.day)
    q_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
    src_start, src_end = placed_window(q_start, q_end)

    lines = db.execute(
        select(OrderLine.sku, OrderLine.quantity, Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= src_start)
        .where(Order.placed_at < src_end)
    ).all()

    units: dict[str, list[int]] = defaultdict(lambda: [0] * n_cols)
    col_totals = [0] * n_cols
    for sku, qty, placed in lines:
        local = placed_local(placed)
        bucket = local.weekday() if dim == "dow" else _daypart_bucket(local.hour)
        units[sku][bucket] += qty
        col_totals[bucket] += qty

    total_skus = len(units)
    catalog = {s.tiktok_sku_id: (s.sku, s.name)
               for s in db.execute(select(Sku).where(Sku.tiktok_sku_id.isnot(None))).scalars()}

    def _code(sku: str) -> str:
        return catalog.get(sku, ("Unmapped", ""))[0]

    ranked = sorted(units.items(), key=lambda kv: (-sum(kv[1]), _code(kv[0])))[:top_n]

    rows: list[HeatRow] = []
    for sku, buckets in ranked:
        code, name = catalog.get(sku, ("Unmapped", f"Unmapped SKU {sku}"))
        row_peak = max(buckets)
        cells = []
        for i, u in enumerate(buckets):
            if u == 0 or row_peak == 0:
                level = 0
            else:
                level = 1 + floor((u / row_peak) * (HEAT_LEVELS - 2))
                level = max(1, min(level, HEAT_LEVELS - 1))
            cells.append(HeatCell(bucket=i, label=columns[i], units=u, level=level))
        peak_i = max(range(n_cols), key=lambda i: buckets[i]) if row_peak > 0 else None
        rows.append(HeatRow(sku_id=sku, code=code, name=name, total_units=sum(buckets),
                            cells=cells, peak_label=(columns[peak_i] if peak_i is not None else "")))

    busiest_col = columns[max(range(n_cols), key=lambda i: col_totals[i])] if any(col_totals) else None

    return HeatmapView(columns=columns, rows=rows, dim=dim, total_skus=total_skus,
                       shown=len(rows), busiest_col=busiest_col,
                       window_start=start, window_end=end)
```

- [ ] **Step 4: Run the tests** — `py -m pytest tests/test_sku_time_heatmap.py -v 2>&1 | tail -20`. Expected: 6 passed. Do NOT alter the assertions to pass. Confirm `OrderLine.sku`/`quantity`/`order_id` and `Sku.tiktok_sku_id`/`sku`/`name` names by reading `app/reports/sku_performance.py` (it uses the same join); if any differ, STOP and report BLOCKED.

- [ ] **Step 5: Commit** — `.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
heatmap: SKU x time compute module

compute_sku_time_heatmap aggregates PAID units per SKU by shop-local weekday or
daypart, ranks top-N by total units, and assigns per-row colour levels (each SKU
shaded against its own peak bucket) + a peak label + busiest column. Pure
computation.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/reports/sku_time_heatmap.py tests/test_sku_time_heatmap.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`

---

## Task 2: Route + Heatmap tab template

**Files:**
- Modify: `app/routers/reports.py` (`sales_view`)
- Modify: `app/templates/reports/sales.html`
- Test: `tests/test_sales_heatmap_tab.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_sales_heatmap_tab.py`:

```python
# tests/test_sales_heatmap_tab.py
"""The Heatmap tab on /reports/sales renders the grid + dim toggle; the tab is a
real link; Overview/SKUs/Timing are unaffected."""
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


def _seed(db):
    db.add(Sku(sku="SBX-1", name="Primer", brand="smashbox", tiktok_sku_id="S1", unit_cogs=Decimal("0")))
    db.flush()
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime.now().replace(hour=12, minute=0, second=0, microsecond=0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox", gross_sales=Decimal("50"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=5, gross_sales=Decimal("50")))
    db.flush()


def test_heatmap_tab_renders(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=heatmap")
    assert r.status_code == 200
    assert "SBX-1" in r.text                 # the SKU row
    assert "Day of week" in r.text           # the dim toggle
    assert "tab=heatmap" in r.text           # the tab is a real link


def test_heatmap_daypart_switch(client):
    with SessionLocal() as db:
        _seed(db); db.commit()
    r = client.get("/reports/sales?tab=heatmap&dim=daypart")
    assert r.status_code == 200
    assert "Morning" in r.text and "Evening" in r.text   # daypart columns


def test_other_tabs_unaffected(client):
    assert "Revenue velocity" in client.get("/reports/sales").text
    assert "Showing" in client.get("/reports/sales?tab=skus").text
    assert "Day of week" in client.get("/reports/sales?tab=timing").text
```

- [ ] **Step 2: Run to verify failure** — `py -m pytest tests/test_sales_heatmap_tab.py -v 2>&1 | tail -15`. Expected: `test_heatmap_tab_renders` + `test_heatmap_daypart_switch` FAIL (no heatmap block); `test_other_tabs_unaffected` passes.

- [ ] **Step 3: Extend the route** — in `app/routers/reports.py`, `sales_view`:

(a) Add a `dim` param to the signature (after `page: int = 1,`):
```python
               per_page: int = DEFAULT_PER_PAGE, page: int = 1, dim: str = "dow",
```
(b) Change the tab-normalization line to accept "heatmap":
```python
    ctx["tab"] = tab if tab in ("skus", "timing", "heatmap") else "overview"
```
(c) After the `elif ctx["tab"] == "timing":` block (after its last line building `daily_chart`), add:
```python
    elif ctx["tab"] == "heatmap":
        from app.reports.sku_time_heatmap import compute_sku_time_heatmap
        v = ctx["view"]
        ctx["heatmap"] = compute_sku_time_heatmap(db, start=v.window_start, end=v.window_end, dim=dim)
        ctx["dim"] = ctx["heatmap"].dim
```
Do NOT change `_sales_view_data`, the SKU/timing branches, or the CSV route.

- [ ] **Step 4: Update the template** — `app/templates/reports/sales.html`. READ it first.

(a) **Add the 4th tab link** in the tab bar (after the Timing `<a>`):
```html
  <a href="/reports/sales?{{ period_qs }}&tab=heatmap"
     class="-mb-px border-b-2 px-3 py-2 font-medium {{ 'border-slate-900 text-slate-900' if tab == 'heatmap' else 'border-transparent text-slate-500 hover:text-slate-800' }}">Heatmap</a>
```

(b) **Add the Heatmap block** immediately before the final `{% endblock %}` (after the Timing tab's closing `{% endif %}`):
```html
{% if tab == 'heatmap' %}
{# ── Heatmap tab — SKU × time (units) ──────────────────────────────────── #}
{% set heat = {
  0:'bg-slate-50 text-slate-300', 1:'bg-indigo-100 text-indigo-800',
  2:'bg-indigo-300 text-indigo-900', 3:'bg-indigo-500 text-white',
  4:'bg-indigo-700 text-white' } %}
<div class="mb-4 flex flex-wrap items-center gap-1 text-sm print:hidden">
  <span class="mr-1 text-xs text-slate-400">Group by</span>
  <a href="/reports/sales?{{ period_qs }}&tab=heatmap&dim=dow"
     class="rounded-md px-3 py-1.5 font-medium {{ 'bg-slate-900 text-white' if heatmap.dim == 'dow' else 'text-slate-600 hover:bg-slate-100' }}">Day of week</a>
  <a href="/reports/sales?{{ period_qs }}&tab=heatmap&dim=daypart"
     class="rounded-md px-3 py-1.5 font-medium {{ 'bg-slate-900 text-white' if heatmap.dim == 'daypart' else 'text-slate-600 hover:bg-slate-100' }}">Daypart</a>
</div>
<div class="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
  <div>
    {% if heatmap.shown %}Busiest {{ 'day' if heatmap.dim == 'dow' else 'daypart' }}:
      <span class="font-semibold text-slate-700">{{ heatmap.busiest_col }}</span>
      · top {{ heatmap.shown }} of {{ heatmap.total_skus }} SKUs by units{% else %}No SKU sales in this period.{% endif %}
  </div>
  <div class="flex items-center gap-1 text-[10px] text-slate-400">
    <span>Less</span>
    {% for lv in range(5) %}<span class="inline-block h-3 w-3 rounded-sm {{ heat[lv] }}"></span>{% endfor %}
    <span>More <span class="text-slate-300">(per SKU)</span></span>
  </div>
</div>
<div class="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
  <table class="min-w-full text-sm">
    <thead>
      <tr class="border-b border-slate-200 text-[11px] uppercase tracking-wider text-slate-500">
        <th class="px-3 py-2 text-left font-semibold">SKU</th>
        {% for c in heatmap.columns %}<th class="px-2 py-2 text-center font-semibold">{{ c }}</th>{% endfor %}
        <th class="px-3 py-2 text-left font-semibold">Peak</th>
      </tr>
    </thead>
    <tbody>
      {% for r in heatmap.rows %}
      <tr class="border-b border-slate-100">
        <td class="px-3 py-2"><div class="font-medium text-slate-800">{{ r.code }}</div><div class="truncate text-[11px] text-slate-500">{{ r.name }}</div></td>
        {% for cell in r.cells %}
        <td class="px-1 py-1 text-center">
          <div class="rounded px-2 py-1 text-[11px] font-medium tabular-nums {{ heat[cell.level] }}" title="{{ cell.label }}: {{ cell.units }}">{{ cell.units or '' }}</div>
        </td>
        {% endfor %}
        <td class="px-3 py-2 text-[11px] font-medium text-slate-600">{{ r.peak_label }}</td>
      </tr>
      {% else %}
      <tr><td colspan="{{ heatmap.columns | length + 2 }}" class="px-3 py-6 text-center text-slate-400">No SKU sales in this period.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}
```
(The new block ends `{% endif %}`, then the existing `{% endblock %}` follows.)

CRITICAL: all the new markup is inside `{% if tab == 'heatmap' %}` (heatmap/dim vars are only set on this tab). Confirm tag balance: the four `{% if tab == ... %}` blocks each have a matching `{% endif %}`, and the file ends `{% endblock %}`.

- [ ] **Step 5: Run the tests**
- `py -m pytest tests/test_sales_heatmap_tab.py -v 2>&1 | tail -15` → all 3 pass.
- Regression: `py -m pytest tests/test_sales_page.py tests/test_sales_skus_tab.py tests/test_sku_pagination.py tests/test_sales_timing_tab.py -q 2>&1 | tail -8` → all pass (the other three tabs unaffected).

- [ ] **Step 6: Commit** — `.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
heatmap: enable + render the Heatmap tab

Add the 4th Heatmap tab; a tab==heatmap branch in sales_view (dim=dow|daypart);
the Heatmap template block — dim toggle, busiest-column caption + per-SKU legend,
and the colour-scaled SKU × time grid with a Peak column.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/routers/reports.py app/templates/reports/sales.html tests/test_sales_heatmap_tab.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`

---

## Task 3: Full suite + deploy + verify

- [ ] **Step 1:** `py -m pytest 2>&1 | tail -12` → all pass (894 + the new heatmap tests).
- [ ] **Step 2: Merge + deploy (local-merge, no PR):**
```bash
git push -u origin feature/sku-time-heatmap
git checkout main && git pull --ff-only
git merge --no-ff feature/sku-time-heatmap -m "Merge feature/sku-time-heatmap"
git push origin main
git branch -d feature/sku-time-heatmap && git push origin --delete feature/sku-time-heatmap
fly deploy
```
No schema change → release `alembic upgrade head` is a no-op.
- [ ] **Step 3: Verify** — `fly releases` healthy; `curl … /healthz` → 200; `curl … "/reports/sales?tab=heatmap"` → 303 (auth)/route registered. Then ask the user for the eyeball pass (desktop + phone): the grid colours per row, the DOW/Daypart toggle switches columns, the Peak column + caption read right.

---

## Self-Review

**Spec coverage:** top-20 SKU × {dow|daypart} grid of units (`compute_sku_time_heatmap`, top_n=20) ✓; switchable dim (`dim` param + toggle) ✓; per-row leveling (`row_peak`, 0..4) ✓; new Heatmap tab + route branch ✓; Peak column + busiest-col caption + per-SKU legend ✓; PAID-only, unmapped→"Unmapped", empty/few-SKU/invalid-dim edges ✓; literal-class colour dict (indigo, safelisted) ✓; units only, no money ✓.

**Placeholder scan:** none — full module + route + template + tests provided.

**Type consistency:** route ctx keys (`heatmap`, `dim`) match template refs; `HeatmapView` fields (`columns`, `rows`, `dim`, `total_skus`, `shown`, `busiest_col`) and `HeatRow`/`HeatCell` fields (`code`, `name`, `cells`, `peak_label`, `cell.level`, `cell.units`, `cell.label`) used identically in template + tests; `compute_sku_time_heatmap(db, *, start, end, dim, top_n)` signature matches the route call and tests; `heat[cell.level]` indices 0..4 match the module's level range. ✓
