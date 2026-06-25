# Sales report — per-SKU drill-down page (sub-project C)

Third of A→B→C. A sales-lens page per SKU that composes the signals built in
A (cover) and B (refund) plus a longer trend and recent activity. Cross-links
to the existing demand-planner drill-down (the buying lens).

## Route & entry

- `GET /reports/sales/sku/{sku_id}` — `sku_id` is the table row's `sku_id`
  (canonical TikTok SKU ID / raw key). Carries the period query-string
  (granularity / start+end / fiscal year+month) so the headline stats match
  the period the user clicked from. 404 → render a friendly "no data" page.
- Entry: in the SKUs table, the SKU code becomes a link to this page (period
  appended). The inline ▸ stats expander stays.

## Data — `compute_sales_sku_detail(db, sku_id, *, start, end, as_of=None)`

Returns `SalesSkuDetail`:
- `row: SkuPerfRow | None` — pulled from `compute_sku_performance(db, start, end)`
  (reuse: gives stats, cover, refund, momentum, status for the selected period).
  None when the SKU has no activity in the period.
- `weekly_trend: list[WeekPoint]` — last **12 ISO weeks** (fixed, independent of
  the selected period): `week_start`, `units`, `revenue`. One grouped query over
  PAID lines for this `sku_id`, bucketed in Python (SQLite-portable, like the
  planner's `_weekly_velocity`). Sales-lens = order-line `sku` level (NOT
  bundle-expanded), matching the table.
- `recent_orders: list[RecentOrder]` — last **20** orders containing the SKU:
  `placed_at`, `tiktok_order_id`, `qty`, `gross`, `net`, `refunded` (order
  refunds > 0). Order by `placed_at` desc.
- `bundle_parents`, `bundle_components` — reuse the planner's
  `_bundle_relationships(db, sku_obj, sku_id)`.

`as_of` defaults to today; the 12-week window anchors on it.

## UI — `reports/sales_sku_detail.html`

- Header: SKU code + name + the period label; a "View in demand planner →" link
  to `/reports/demand-planning/sku/{sku_id}`.
- Stats panel: the granular stats + days-of-cover + refund rate (reuse the tile
  layout from the inline expander).
- Trend: a 12-week bar chart (units) with revenue in the tooltip — reuse
  `dashboard_trends.bar_chart` / the sparkline helpers already used on the page.
- Recent orders: a compact table (date, order id, qty, gross, net, refunded ✓).
- Bundle membership: parents (bundles this SKU sells inside) and, if it's a
  bundle, its components.
- Empty/None states for each section.

## Testing (TDD)

- `compute_sales_sku_detail`: seeded SKU with sales across weeks + a refund + a
  bundle → assert row present (stats/cover/refund), weekly_trend buckets units +
  revenue, recent_orders ordered desc with the refunded flag, bundle parents.
  None-row case for an inactive SKU.
- Route: `/reports/sales/sku/{id}` 200 + renders the SKU name, a trend, recent
  orders, and the planner cross-link; unknown id → graceful page.
- Table: SKU code links to the drill-down (smoke).

## Files

- `app/reports/sku_performance.py` (or a new `sales_sku_detail.py`) —
  `SalesSkuDetail` + `compute_sales_sku_detail` + weekly/recent helpers.
- `app/routers/reports.py` — the route.
- `app/templates/reports/sales_sku_detail.html` — new page; `sales.html` — link.
- `tests/` — detail computation + route + table-link smoke.

## Out of scope

Refund trend over time; editing from the page; pagination of recent orders
(fixed last-20).
