# SKU × Time Heatmap (Phase 3 of SKU Sales Analytics) — Design

**Date:** 2026-06-23
**Status:** Approved (design)

## Context

Final phase of the three-part SKU Sales Analytics build on `/reports/sales`:
1. **SKU Performance** (shipped) — per-SKU units/sales/momentum/lifecycle (SKUs tab).
2. **Temporal patterns** (shipped) — aggregate time-of-sale patterns (Timing tab).
3. **SKU × time heatmap (this spec)** — *which SKUs sell when*.

The tab bar today is **Overview · SKUs · Timing**. This adds a 4th **Heatmap** tab.
Data is available: `Order.placed_at` → `reporting_tz.placed_local()` (shop-local
weekday + hour), `OrderLine.sku`/`quantity`, the `OrderLine.sku → Sku` join, PAID
orders only.

## Decisions (from brainstorming)

- **Switchable time dimension**: **Day of week** (Mon–Sun, 7 cols) or **Daypart**
  (Morning/Afternoon/Evening/Night, 4 cols), via a `?dim=` toggle. (No 24-col hour
  heatmap — too wide; the Timing tab already covers hour aggregate.)
- **Metric: units** (Σ `OrderLine.quantity`). Clean per-SKU attribution; the
  per-order GMV can't be split across a line cleanly, so revenue is out.
- **Per-row color scaling**: each cell shades against *that SKU's own peak bucket*,
  so every SKU's hottest time is visible regardless of its total volume.
- **Rows = top 20 SKUs by total units** in the window (note "top 20 of N").
- **New "Heatmap" tab.**

## Architecture

### 1. Report module — `app/reports/sku_time_heatmap.py` (new)

Pure computation (reads ORM, returns dataclasses), mirroring the sibling reports.

```python
DIMS = ("dow", "daypart")
HEAT_LEVELS = 5                # 0 (none) … 4 (peak)

@dataclass
class HeatCell:
    bucket: int                # column index (0-based)
    label: str                 # "Mon" / "Morning"
    units: int
    level: int                 # 0..4, per-row intensity bucket

@dataclass
class HeatRow:
    sku_id: str                # canonical OrderLine.sku
    code: str                  # Sku.sku (SBX-form) or "Unmapped"
    name: str                  # Sku.name or "Unmapped SKU <id>"
    total_units: int           # row total across all buckets (sort key)
    cells: list[HeatCell]      # one per column, in column order
    peak_label: str            # the row's busiest bucket label ("Sat" / "Evening"); "" if no sales

@dataclass
class HeatmapView:
    columns: list[str]         # column header labels
    rows: list[HeatRow]        # top_n by total_units, desc (ties by code)
    dim: str                   # "dow" | "daypart"
    total_skus: int            # distinct SKUs with sales in the window
    shown: int                 # len(rows)
    busiest_col: str | None    # column with the highest total units across all SKUs
    window_start: date
    window_end: date

def compute_sku_time_heatmap(db, *, start, end, dim="dow", top_n=20) -> HeatmapView
```

**Columns / bucketing** (`placed_local(order.placed_at)`):
- `dim == "dow"`: 7 columns Mon..Sun; `bucket = local.weekday()`.
- `dim == "daypart"`: 4 columns Morning/Afternoon/Evening/Night;
  `bucket = daypart_of(local.hour)` — Morning 05:00–11:59, Afternoon 12:00–16:59,
  Evening 17:00–21:59, Night 22:00–04:59 (same partition as `temporal_patterns`).
- Invalid `dim` → "dow".

**Aggregation (PAID only):** select per-line `(OrderLine.sku, OrderLine.quantity,
Order.placed_at)` for PAID orders placed in the shop-local window (source bounds via
`placed_window`). Accumulate `units[sku][bucket] += quantity`. Resolve `sku` →
`Sku.sku`/`Sku.name` (unmapped → "Unmapped").

**Rows:** rank SKUs by `total_units` desc (ties by `code`), take `top_n`. For each
row build a `HeatCell` per column. **Per-row level:** `row_peak = max(cell.units)`;
`level = 0` if `units == 0` else `1 + floor((units / row_peak) * (HEAT_LEVELS - 2) )`
clamped to `[1, 4]` (so any non-zero cell is ≥ level 1 and the peak is level 4).
`peak_label` = the label of the max-units cell (first on ties).

**Insights:** `busiest_col` = column with the greatest total units summed across *all*
SKUs in the window (not just the shown rows); `None` when there are no sales.

### 2. Route — extend `sales_view` (`app/routers/reports.py`)

Add `dim: str = "dow"` param and a `tab == "timing"`-style branch:
```python
elif ctx["tab"] == "heatmap":
    from app.reports.sku_time_heatmap import compute_sku_time_heatmap
    v = ctx["view"]
    ctx["heatmap"] = compute_sku_time_heatmap(db, start=v.window_start, end=v.window_end, dim=dim)
    ctx["dim"] = ctx["heatmap"].dim
```
Tab normalization extends to accept `"heatmap"`. Overview/SKUs/Timing and the CSV
route are unchanged. No pagination (top-N grid).

### 3. Template — `app/templates/reports/sales.html`

- **Add the 4th tab** "Heatmap" (real link, active when `tab=='heatmap'`).
- **`{% if tab == 'heatmap' %}` block:**
  - **Dim toggle** — Day of week / Daypart links (`?…&tab=heatmap&dim=dow|daypart`,
    carrying `period_qs`), active state styled like the scope toggles.
  - **Caption** — "Busiest {busiest_col}: … · top {shown} of {total_skus} SKUs by units"
    (or an empty-state line when no sales).
  - **Heatmap table** (`overflow-x-auto`): a header row (SKU · the columns · Peak);
    one row per `HeatRow` — the SKU code+name, then one cell per `HeatCell` whose
    background is a **literal** Tailwind class chosen by `cell.level` (a Jinja dict of
    full class strings, e.g. `{0:'bg-slate-50 text-slate-300', 1:'bg-indigo-100
    text-indigo-800', 2:'bg-indigo-300 text-indigo-900', 3:'bg-indigo-500 text-white',
    4:'bg-indigo-700 text-white'}`), showing the unit count; then the `peak_label`.
  - Indigo shades 100/300/500/700 are already covered by the `tailwind.config.js`
    safelist, so they compile regardless; the dict uses whole literal strings anyway.

## Data flow

```
/reports/sales?tab=heatmap&dim=dow&<period params>
  → resolve window (existing _sales_view_data) → window_start/_end
  → compute_sku_time_heatmap(db, start, end, dim)
       PAID line units → placed_local bucket → units[sku][bucket]; top-N rows;
       per-row 0..4 levels; busiest column
  → render sales.html Heatmap tab (toggle + caption + colored grid)
```

## Error handling / edge cases

- **Empty window** → `rows=[]`, `total_skus=0`, `busiest_col=None`; the table shows an
  empty-state line. No divide-by-zero (guard `row_peak > 0`).
- **Unmapped SKU** (no `Sku` row) → code/name "Unmapped" (+ raw id), still ranked.
- **Fewer than `top_n` SKUs** → show all.
- **A SKU with sales only in some buckets** → empty buckets render level 0 (faint),
  count 0 (or blank).
- **Invalid `dim`** → "dow".
- Units are ints; no money/Decimal in this report.

## Testing

Compute (`tests/test_sku_time_heatmap.py`) — seed PAID orders/lines at chosen
local timestamps (assert buckets via `placed_local`, DST-robust):
- a SKU's units land in the correct **weekday** column (`dim="dow"`) and the correct
  **daypart** column (`dim="daypart"`).
- **per-row leveling**: a SKU with one big bucket + one small bucket → big = level 4,
  small ≥ level 1, empty = level 0; a low-volume SKU still reaches level 4 in its own
  peak (per-row, not global).
- **top-N**: more than `top_n` SKUs → only the top `top_n` by total units returned;
  ranking + `total_skus` correct.
- **peak_label** + **busiest_col** correct; unmapped SKU → "Unmapped"; PAID-only
  (a SAMPLE order excluded); empty window → empty grid, `busiest_col` None.
- invalid `dim` → "dow".

Route/render (`tests/test_sales_heatmap_tab.py`):
- `?tab=heatmap` renders the grid + toggle; `?dim=daypart` switches columns; the
  Heatmap tab is a real link; Overview/SKUs/Timing unaffected; period scopes drive
  the window.

## Out of scope (deliberately)

- Hour-of-day (24-col) heatmap — the toggle is DOW/Daypart only.
- Revenue metric (units only).
- Heatmap CSV export; per-cell drill-down; configurable `top_n` in the UI.

## Success criteria

1. The Heatmap tab shows a top-20 SKU × {day-of-week | daypart} grid of unit counts,
   color-scaled **per row** so each SKU's best time is visible, with a working
   dimension toggle over the selected period.
2. A caption surfaces the busiest column + the shown/total SKU count; a Peak column
   labels each SKU's hottest bucket.
3. Edge cases (empty / few-SKU / unmapped) degrade gracefully; Overview/SKUs/Timing
   and the full suite stay green.
