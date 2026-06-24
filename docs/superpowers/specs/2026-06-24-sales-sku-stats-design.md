# Sales report — granular per-SKU statistics

## Goal

Add rate/cadence/forecast statistics to the per-SKU view of the Sales report
(`/reports/sales`, SKUs tab), over the currently-selected period. The existing
table shows totals (Units, Net Sales, Orders, % of Units, Momentum, Status);
this adds the *behavioral* metrics that totals hide. Stats appear in an
expandable per-SKU detail panel (table stays lean) **and** in the SKU CSV.

Out of scope (explicitly not selected): gross margin, ASP, discount/unit.

## Metrics

All computed for the selected window `[window_start, window_end]` (inclusive),
`window_days = (window_end − window_start).days + 1`. Active rows only (SKUs with
≥1 unit in the window), matching the on-screen table.

| Metric | Formula | Notes / edge cases |
|---|---|---|
| Avg units/day (calendar) | units ÷ window_days | headline rate |
| Avg units/day (selling) | units ÷ days_with_sales | "—" if days_with_sales = 0 (can't happen for active rows, but guard) |
| Days active | count of distinct days with units > 0 | integer |
| % days active | days_with_sales ÷ window_days × 100 | 1-dp |
| Avg revenue/day | net_sales ÷ window_days | 2-dp dollars |
| Avg units/order | units ÷ orders | "—" if orders = 0 |
| Run-rate (30d) | avg_units_per_day(calendar) × 30 | rounded to whole units |
| Best day | max single-day units, + that date | date `None` if no sales |
| Volatility (CoV) | population std-dev ÷ mean of the **zero-filled** daily-units series | `None` if window_days < 2 or mean = 0; matches the demand planner's zero-filled basis for consistency |

Decimal precision: rates 0.01, percentages 0.1, run-rate whole units.

## Data model

New dataclass in `app/reports/sku_performance.py`:

```python
@dataclass
class SkuStats:
    window_days: int
    days_with_sales: int
    pct_days_active: Decimal
    avg_units_per_day: Decimal            # calendar basis (headline)
    avg_units_per_selling_day: Decimal | None
    avg_revenue_per_day: Decimal
    avg_units_per_order: Decimal | None
    run_rate_30d: int
    best_day_units: int
    best_day_date: date | None
    volatility_cov: Decimal | None
```

`SkuPerfRow` gains `stats: SkuStats`.

## Computation

`compute_sku_performance` already aggregates per-SKU totals over the window in
one pass. Add one grouped query for the daily breakdown:

```
SELECT order_line.sku, date(order.placed_at) AS d,
       SUM(quantity) AS units, SUM(<net expr>) AS net
FROM order_lines JOIN orders ...
WHERE <PAID/Completed/Shipped, placed_at in window>
GROUP BY sku, d
```

Build `{sku: {date: units}}` and `{sku: {date: net}}`, then derive `SkuStats`
per active SKU (zero-fill across `window_days` for the CoV series). Same
filters/window the totals already use, so the daily sums reconcile to the totals
exactly. Pure function, no new endpoint, no N+1.

## UI — expandable detail

- Each SKU row gets an expand control (chevron button, `aria-expanded`) in the
  first cell.
- A sibling detail row (`<tr>` spanning all columns, hidden by default) holds a
  labeled stats grid rendered from `row.stats`.
- Toggle is client-side vanilla JS (show/hide the sibling row) — stats are
  already in the rendered HTML, so no server round-trip.
- Existing columns and pagination are unchanged.

## CSV

Append stats columns to `SKU_CSV_HEADER` / `sku_performance_csv_rows`:

`Avg Units/Day`, `Avg Units/Day (Selling)`, `Days Active`, `% Days Active`,
`Avg Revenue/Day`, `Avg Units/Order`, `Run-Rate (30d)`, `Best Day Units`,
`Best Day Date`, `Volatility (CoV)`.

Empty-ish values (`None`) render as blank cells.

## Testing (TDD)

- `compute_sku_performance` stats: known fixture (e.g. a SKU selling on 3 of 10
  window days) → assert avg/day (both bases), days_active + %, avg revenue/day,
  units/order, run-rate, best-day (units + date), CoV value.
- Edge cases: single-day window (avg = total, CoV None); SKU with one selling
  day (CoV None); orders math.
- Daily sums reconcile to existing totals (guard against double-count).
- CSV: `/reports/sales.csv?tab=skus` includes the new headers + a known row's
  values. Page renders (smoke) with the detail row present.

## Files touched

- `app/reports/sku_performance.py` — `SkuStats`, computation, CSV rows/header.
- `app/routers/reports.py` — none expected (route already passes window+tab).
- `app/templates/reports/sales.html` — expand control + detail row + toggle JS.
- `tests/` — new stats tests; extend `test_sales_sku_csv.py`.
