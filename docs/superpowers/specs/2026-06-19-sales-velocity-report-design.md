# Sales Velocity Report — Design

**Date:** 2026-06-19
**Status:** Approved (design)

## Problem

`app/reports/sales_report.py` already computes a sales-velocity view (revenue /
units / orders at daily / weekly / monthly granularity, with period-over-period
trend, peak, and an in-progress-bucket flag) but was never wired to a route, a
template, the nav, or any test — it has been dead since 2026-05-28. We want to
finish it into a real **Sales** page so the ops team can see revenue velocity and
trend across time, distinct from the Dashboard's current-state KPIs.

The computation module is sound: every `Order` field and `reporting_tz` helper it
references exists, and its revenue formula targets the canonical Seller-Center
GMV. So this is a wiring job (page + nav + CSV + tests) plus a small cleanup — no
computation rewrite.

## Scope decisions (from brainstorming)

- **Nav:** a top-level **"Sales"** link (not tucked in a dropdown).
- **Chart:** an inline-SVG revenue bar chart, matching the bespoke inline-SVG
  trend charts already in `pnl.html` (no chart library).
- **Export:** CSV (not Excel).
- **Access:** a `/reports/*` page — available to every authenticated user, not
  admin-gated (consistent with P&L / Ad-Spend).

## Reused as-is

`compute_sales_report(db, granularity="daily", *, as_of=None) -> SalesReportView`
in `app/reports/sales_report.py`. Returns:

- `granularity`, `buckets: list[SalesBucket]` (chronological, includes zero-sale
  buckets), `total_revenue`, `total_units`, `total_orders`, `avg_aov`,
  `window_start`, `window_end`, `days_in_window`, `avg_daily_revenue`,
  `avg_daily_units`, `revenue_delta`, `units_delta`, `peak`, `as_of`.
- `SalesBucket`: `key`, `label`, `start`, `revenue` (canonical GMV), `units`,
  `orders`, `in_progress`, and a computed `aov` property.

Default windows per granularity: daily = last 30 days, weekly = last 12 ISO
weeks, monthly = last 12 calendar months. Revenue per order =
`gross_sales + shipping_revenue − seller_funded_outlandish −
seller_funded_smashbox − platform_discount_total − payment_platform_discount`
(PAID orders only, bucketed by shop-local placed date).

### Cleanup (part of this work)

Delete the two dead helpers in `sales_report.py`:
- `_is_weekly_key` — a stub that always returns `False`, with a comment admitting
  it's "overkill"; not meaningfully used.
- `_bucket_days` — defined but never called by `compute_sales_report` (its only
  caller would have been a per-bucket velocity display that doesn't exist).

Removing them changes no behaviour (confirmed: `compute_sales_report` does not
call either). The existing compute tests (added in this work) must still pass.

## New components

### 1. Router — `app/routers/reports.py`

- `GET /reports/sales` — reads `granularity` query param (default `"daily"`;
  invalid value falls back to `"daily"`, already handled inside
  `compute_sales_report`), computes the view, renders `reports/sales.html` with
  `{"view": view, "granularities": GRANULARITIES}`. Uses the established
  `templates.TemplateResponse(request, "reports/sales.html", ctx)` form.
- `GET /reports/sales.csv` — same `granularity` param, same view object, streams
  a CSV with header `["Period", "Start", "Revenue", "Units", "Orders", "AOV",
  "In Progress"]` and one row per bucket. Mirrors the existing `ad-spend.csv`
  streaming pattern (a `Response`/`StreamingResponse` with
  `content-disposition: attachment; filename=sales_<granularity>.csv`).

Both reuse the same `compute_sales_report` call so the page and the export can
never diverge.

### 2. Template — `app/templates/reports/sales.html`

Extends the app's base layout and uses the shared `ui` macros + `money` filter.
Sections, top to bottom:

1. **Header**: title "Sales" + a one-line subtitle naming the window
   (`view.window_start` … `view.window_end`) and "PAID orders, Seller-Center GMV".
2. **Granularity toggle**: Daily / Weekly / Monthly — three links to
   `/reports/sales?granularity=…`; the active one is visually selected.
3. **Summary cards** (a responsive grid): Total Revenue, Total Units, Total
   Orders, Avg AOV, Avg Daily Revenue; plus the **revenue** and **units** trend
   deltas (rendered with the existing delta-chip ▲/▼ convention, suppressed when
   the delta is `None`), and the **peak** bucket (label + revenue) when present.
4. **Inline-SVG revenue bar chart**: one bar per bucket scaled to the max bucket
   revenue, x-labels thinned to avoid crowding; the `in_progress` bucket rendered
   muted/striped. Bespoke inline SVG, mirroring `pnl.html`'s trend-chart markup
   (no chart library; not an icon — exempt from the icon guard).
5. **Velocity table**: columns Period · Revenue · Units · Orders · AOV, one row
   per bucket in chronological order, zero-sale buckets included, the in-progress
   row tagged "in progress".
6. **Download CSV** button linking to `/reports/sales.csv?granularity=…` for the
   current granularity.

### 3. Nav — `app/templates/partials/nav.html`

Add `("/reports/sales", "Sales")` to the `primary_links_left` list so it renders
as a top-level link between **P&L** and **Action Center**.

## Data flow

```
GET /reports/sales?granularity=weekly
  → compute_sales_report(db, "weekly")  → SalesReportView
  → render reports/sales.html

GET /reports/sales.csv?granularity=weekly
  → compute_sales_report(db, "weekly")  → SalesReportView
  → stream rows (one per bucket)
```

## Error handling / edge cases (already handled in the module)

- Invalid / missing `granularity` → defaults to `"daily"`.
- Empty data → every bucket in the window still renders at zero (continuous
  series; no crash).
- In-progress bucket (the one containing today) is flagged and **excluded** from
  the trend delta; `prior_has_data=False` suppresses a misleading delta when the
  prior period had no orders.
- `peak` is `None` when all buckets are zero (template guards it).

## Testing

No network; SQLite test DB; seed `Order`/`OrderLine` rows.

**Compute (`tests/test_sales_report.py`):**
- Daily bucketing: orders on distinct days land in distinct daily buckets; revenue
  equals the GMV formula for a seeded order.
- Weekly/monthly bucketing: orders within the same ISO week / calendar month roll
  into one bucket.
- Trend delta excludes the in-progress bucket (seed today + two prior complete
  periods; assert the delta compares the two complete ones).
- Peak detection picks the highest-revenue bucket; `peak is None` when all zero.
- Empty window → all buckets present at zero, totals zero, deltas `None`.
- **Parity:** monthly sales revenue for a settled month ties to that month's
  `MonthlyPnL.gmv` (locks the "ties to the dashboard" claim).

**Route (`tests/test_sales_page.py`):**
- `GET /reports/sales` → 200, shows "Sales" + the daily window; toggling
  `?granularity=monthly` switches the heading/window.
- Invalid `granularity=foo` → 200, falls back to daily (no error).
- `GET /reports/sales.csv` → 200, `text/csv`, attachment, header row exact, one
  data row per bucket.
- Nav: the rendered page contains a top-level link to `/reports/sales`.

## Out of scope (YAGNI)

- Fiscal-period variant of the window.
- Per-SKU / per-creator drilldown.
- Excel (.xlsx) export — CSV only.
- Custom date-range picker — the fixed per-granularity windows (30d / 12wk /
  12mo) are v1.

## Success criteria

1. A top-level **Sales** nav link opens `/reports/sales` with summary cards, the
   inline-SVG revenue chart, and the velocity table; the granularity toggle works.
2. **Download CSV** returns the velocity table for the active granularity.
3. Monthly revenue ties to `MonthlyPnL.gmv` for a settled month.
4. Dead helpers removed; full test suite green.
