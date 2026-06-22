# SKU Performance Report (Phase 1 of SKU Sales Analytics) — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Context

The `/reports/sales` page today is a single overall-velocity view (revenue/units/
orders by day/week/month, with custom-range + fiscal scopes). The user wants
SKU-level sales analytics. The full ask spans three dimensions — decomposed into
three sub-projects, built in order:

1. **SKU Performance (this spec)** — per-SKU units/sales, top sellers, momentum,
   lifecycle status, "act on this" insights.
2. Temporal patterns (day-of-week, hour/daypart, trend shape) — *future*.
3. SKU × time heatmap — *future*.

This spec covers **Phase 1 only**. It also introduces the **tabbed** structure
(`Overview` / `SKUs` / `Timing`) that phases 2–3 will slot into.

Data is fully available: `Order.placed_at` (timestamp), `OrderLine.sku`/`quantity`/
line money fields, `reporting_tz.placed_local_date`, and the existing
`OrderLine.sku → Sku` join (`Sku.tiktok_sku_id == OrderLine.sku`, isouter). PAID
orders only (consistent with the velocity report). The old SKU-*profit* report was
deleted; this is **sales/units performance**, not profit.

## Decisions (from brainstorming)

- **Lifecycle: 6 statuses, ±25% threshold**, vs the immediately-prior equal-length
  window: 🆕 New / 📈 Rising / ➡️ Steady / 📉 Declining / ⏸️ Stalled / 💤 Inactive.
- **Table includes** SKUs sold in the selected window OR the prior window; Inactive
  catalog SKUs are counted always + listed behind a toggle.
- **Structure: tabs** (`Overview` = today's velocity view; `SKUs` = this phase;
  `Timing` = disabled "soon").
- **Per-SKU sparkline**: yes (mini units trajectory per row).
- **Insights strip**: 🏆 top seller · 📈 biggest riser · 📉 biggest faller ·
  🆕 new count · ⏸️ stalled count.
- **Revenue column = Net Sales per SKU** (line-level): `gross_sales −
  platform_discount − seller_funded_outlandish − seller_funded_smashbox`.
- **Default sort: Units desc**; table sortable.
- **"Stalled" = literally zero units** in the selected window (prior window > 0).

## Architecture

### 1. Report module — `app/reports/sku_performance.py` (new)

Pure computation (reads ORM, returns dataclasses), mirroring the existing report
modules.

```python
@dataclass
class SkuPerfRow:
    sku_id: str          # canonical OrderLine.sku (tiktok_sku_id, or raw if unmapped)
    code: str            # Sku.sku (SBX-form) or "Unmapped"
    name: str            # Sku.name or "Unmapped SKU <id>"
    units: int           # selected window
    net_sales: Decimal   # selected window, line-level net
    orders: int          # distinct orders containing the SKU, selected window
    pct_units: Decimal   # units / total units in window, 1 decimal
    prior_units: int     # prior equal-length window
    momentum: Delta | None  # Δ% units vs prior (compute_delta, relative); None when prior == 0
    status: str          # "new"|"rising"|"steady"|"declining"|"stalled"|"inactive"
    spark: str           # sparkline_points of daily units across the window

@dataclass
class SkuInsights:
    top_seller: SkuPerfRow | None      # max units
    biggest_riser: SkuPerfRow | None   # max positive momentum.pct (prior>0, cur>0)
    biggest_faller: SkuPerfRow | None  # min (most negative) momentum.pct
    new_count: int
    stalled_count: int

@dataclass
class SkuPerformanceView:
    rows: list[SkuPerfRow]        # active this-or-prior window, sorted
    inactive_rows: list[SkuPerfRow]
    inactive_count: int
    insights: SkuInsights
    total_units: int
    total_net_sales: Decimal
    window_start: date
    window_end: date

def compute_sku_performance(db, *, start, end, sort="units", as_of=None) -> SkuPerformanceView
```

**Windows:** `L = (end - start).days + 1`; prior window = `[start - L days,
start - 1 day]` (inclusive). Both windows convert shop-local dates → source-tz
bounds via `placed_window` (same as the velocity report).

**Aggregation (PAID only):** group by canonical `OrderLine.sku`:
- selected-window: `units = Σ quantity`, `net_sales = Σ (gross_sales −
  platform_discount − seller_funded_outlandish − seller_funded_smashbox)`,
  `orders = COUNT(DISTINCT order_id)`.
- prior-window: `prior_units = Σ quantity`.
- catalog: all `Sku` rows → those whose `tiktok_sku_id` has zero units in BOTH
  windows are **Inactive**.

**Per-SKU sparkline:** units per shop-local day across `[start, end]` (zero-filled),
fed to `dashboard_trends.sparkline_points`.

**Classification per SKU** (cur = window units, prior = prior-window units):
- `cur == 0 and prior == 0` → **inactive** (catalog only).
- `prior > 0 and cur == 0` → **stalled**.
- first-ever sale of this SKU is within `[start, end]` (no sale before `start`) →
  **new**.
- `cur > 0 and prior == 0` (sold before the prior window, gap, now back) → **rising**.
- `cur > 0 and prior > 0`: Δ% = `(cur−prior)/prior·100` → `> +25%` **rising**,
  `< −25%` **declining**, else **steady**.

**Momentum:** `compute_delta(Decimal(cur), Decimal(prior), prior_has_data=prior>0,
mode="relative")` → the `Delta` (handles prior==0 → "new"). Reuses the existing
delta-chip rendering.

**Sort:** default `units` desc; also `net_sales`, `orders`, `momentum` (by Δ%,
`None`/new sorts last). Applied to `rows` only (inactive list stays name-sorted).

### 2. Route — extend `sales_view` (`app/routers/reports.py`)

Add params: `tab: str = "overview"`, `sort: str = "units"`,
`show_inactive: int = 0`. The window is resolved exactly as today (the existing
`_sales_view_data` already yields `view.window_start/window_end` honoring
granularity/range/fiscal). When `tab == "skus"`, also call
`compute_sku_performance(db, start=view.window_start, end=view.window_end,
sort=sort)` and add `sku` + `tab` + `sort` + `show_inactive` to the context. The
CSV route is unchanged (Phase 1 keeps the existing velocity CSV; a SKU CSV can come
later).

### 3. Template — `app/templates/reports/sales.html`

- **Tab bar** under the period controls: `Overview` · `SKUs` · `Timing` (Timing is
  a disabled "soon" chip). Each enabled tab links to `/reports/sales?tab=…` carrying
  the current period params (granularity / start_date / end_date / year / month).
- **`{% if tab != 'skus' %}`** wraps ALL of today's content (cards + chart + velocity
  table) — Overview is byte-unchanged behavior.
- **`{% else %}` (SKUs tab):**
  - **Insights strip** — 5 compact cards: 🏆 top seller (code + units), 📈 biggest
    riser (code + Δ%), 📉 biggest faller (code + Δ%), 🆕 new count, ⏸️ stalled count.
  - **SKU table** — columns: SKU (code + name) · Units · Net Sales · Orders · % ·
    Δ (the existing `ui.delta_chip`) · Trend (the existing `ui.sparkline`) ·
    Status (a small colored badge per status). Column headers for Units / Net Sales /
    Orders / Δ are **sort links** (`?tab=skus&sort=…` preserving period +
    show_inactive); the active sort is marked. Wrapped in `overflow-x-auto` (mobile).
  - **Inactive control** — "💤 N inactive ▾" toggling `?…&show_inactive=1`; when on,
    the inactive rows render below in a muted block.
- Mobile: the SKU table scrolls horizontally (consistent with the mobile pass);
  the insights strip is a `grid-cols-2 sm:grid-cols-5` responsive grid.

## Data flow

```
/reports/sales?tab=skus&sort=units&<period params>
  → resolve window (existing _sales_view_data) → view.window_start/_end
  → compute_sku_performance(db, start, end, sort)
       selected + prior window aggregation per SKU, classify, sparkline, insights
  → render sales.html SKUs tab
```

## Error handling / edge cases

- Empty window (no PAID sales) → empty rows, zero totals, insights all None/0,
  table shows an empty-state line. No crash.
- Unmapped SKU (no `Sku` row) → `code/name = "Unmapped"` (+ raw id), still ranked.
- Prior window has no data (e.g. earliest period) → every selling SKU is "new" or
  "rising"; momentum shows "new". Correct, not an error.
- Invalid `sort`/`tab` → fall back to defaults (`units` / `overview`).
- `net_sales` is `Decimal`, quantized to cents; never float.

## Testing

Compute (`tests/test_sku_performance.py`) — seed PAID orders + lines across the two
windows:
- per-SKU `units` / `net_sales` (the line-net formula) / `orders` (distinct).
- `prior_units` + `momentum` Δ% vs prior; `pct_units`.
- each of the 6 statuses from purpose-built fixtures (new = first-ever in window;
  rising/declining/steady around ±25%; stalled = prior>0 cur=0; inactive = catalog
  SKU sold in neither window).
- unmapped SKU → "Unmapped"; PAID-only (a SAMPLE order excluded); sparkline non-empty
  for a SKU with sales; insights pick the right top/riser/faller + counts.
- sort param reorders `rows`.

Route (`tests/test_sales_skus_tab.py`):
- `?tab=skus` renders the insights strip + SKU table; `?tab=overview` (default) shows
  today's velocity content unchanged.
- `?tab=skus&sort=net_sales` reorders; the Timing tab chip is present + disabled.
- `?show_inactive=1` reveals the inactive rows; the inactive count shows regardless.
- The existing range/fiscal period scopes still drive the window on the SKUs tab.

## Out of scope (later phases)

- Phase 2: day-of-week / hour-of-day / daypart, trend-shape classification,
  overall-arc characterization.
- Phase 3: SKU × time heatmap.
- A SKU-level CSV export (can add once the table stabilizes).
- Per-SKU profit/COGS (this is sales/units, not profit — the profit report was
  deliberately removed earlier).

## Success criteria

1. The SKUs tab shows a sortable per-SKU table (units / net sales / orders / % /
   momentum Δ / sparkline / lifecycle badge) for the selected period, default
   Units-desc, with the 6-status classification correct.
2. The insights strip surfaces top seller, biggest riser/faller, and new/stalled
   counts.
3. Inactive catalog SKUs are counted + toggle-revealable.
4. Overview tab + all existing period scopes are unchanged; full suite green.
