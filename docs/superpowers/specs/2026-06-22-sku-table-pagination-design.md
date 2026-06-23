# SKU Table Pagination — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Context

The SKUs tab on `/reports/sales` (shipped today, Phase 1) renders the full per-SKU
table in one list. For a catalog of low-hundreds of SKUs that's a long scroll. The
user wants a **page-size control (10/25/50/100) with full pagination** (Prev/Next +
page numbers) on the active SKU table.

## Decisions (from brainstorming)

- **Full pagination**: a 10/25/50/100 size selector PLUS Prev/Next + windowed page
  numbers. (Not a simple top-N cap.)
- **Default page size: 25.**
- **Server-side**, query-param driven — consistent with the page's existing
  `sort`/scope/`show_inactive` round-tripping; no client JS framework.
- **Scope: the active SKU table only.** The inactive-SKUs list stays a full flat
  list behind its toggle (secondary, rarely opened) — unpaged.
- Insights strip, the **"%" column** (% of total units), and totals stay computed
  over the **full** result set — only the table *display* is paged.

## Architecture

### Route — `sales_view` (`app/routers/reports.py`)

Add two params: `per_page: int = 25`, `page: int = 1`. A module constant
`PER_PAGE_OPTIONS = (10, 25, 50, 100)`. Inside the existing `if tab == "skus"`
block, after computing the `sku` view, paginate its already-sorted `.rows`:

- `pp = per_page if per_page in PER_PAGE_OPTIONS else 25` (invalid → 25).
- `total = len(sku.rows)`; `total_pages = max(1, ceil(total/pp))`.
- `pg = clamp(page, 1, total_pages)` (out-of-range clamps; never a stale empty page).
- `page_rows = sku.rows[(pg-1)*pp : (pg-1)*pp + pp]`.
- Context adds: `page_rows`, `per_page`, `page`, `total_rows`, `total_pages`,
  `per_page_options`, `row_start`, `row_end`, and a windowed `page_window` (≤7
  page numbers centered on the current page, computed in Python to keep Jinja simple).

The compute module, CSV route, and Overview tab are untouched.

### Template — `app/templates/reports/sales.html` (SKUs tab)

- A reusable `skus_qs` set carrying period + `tab=skus` + `sort` + `show_inactive`,
  so size/pager/sort/toggle links all preserve state.
- **Above the table**: a control row — left: "Showing {row_start}–{row_end} of
  {total_rows}" (or "Showing 0 of 0" when empty); right: a **[10][25][50][100]**
  size selector (active size highlighted; changing size omits `page` → resets to 1).
- The table iterates **`page_rows`** instead of `sku.rows`.
- **Below the table** (only when `total_pages > 1`): a centered pager — `‹ Prev`,
  the `page_window` numbers (with `1 …` / `… {last}` ellipsis affordances when the
  window doesn't reach the ends), `Next ›`. Disabled Prev/Next render as muted
  spans. Current page highlighted.
- Sort-header links and the inactive-toggle links also carry `&per_page` so the
  chosen size persists across re-sorts and toggles (re-sorting resets to page 1).

## Data flow

```
/reports/sales?tab=skus&sort=units&per_page=25&page=2&<period>
  → resolve window → compute_sku_performance (full sorted rows + insights)
  → slice rows for the page; build pager metadata
  → render: size selector + page_rows table + Prev/numbers/Next pager
```

## Error handling / edge cases

- Invalid `per_page` (not in the option set) → 25.
- `page` < 1 or > total_pages → clamped into range.
- Empty result (no SKUs) → "Showing 0 of 0", no pager, existing empty-state row.
- A single page (`total_pages == 1`) → size selector shown, pager hidden.
- Changing sort or period resets to page 1 (links omit `page`).

## Testing

`tests/test_sku_pagination.py` (TestClient, seed N=30 PAID SKUs, units i for SKU i
so units-desc order is deterministic — `SBX-030` top … `SBX-001` last; names
`ProductName{i}` distinct from codes so table-name assertions aren't polluted by the
insights strip, which renders only codes):

- Default (no params) → page 1, 25 rows: `of 30` shown, top SKU present, the 26th–30th
  (`SBX-001`/`SBX-005`) absent. Default size = 25.
- `per_page=25&page=2` → the remainder present (`SBX-001`), a page-1 SKU (`SBX-006`) absent.
- `per_page=7` (invalid) → falls back to 25 (`SBX-006`, the 25th, present).
- `page=99` → clamps to the last page (`SBX-001` present).
- `per_page=100` → all 30 on one page.
- Overview tab (no `tab=skus`) unaffected — "Revenue velocity" still renders.

Negative assertions deliberately avoid the top-seller code (`SBX-030`), which the
insights strip always renders regardless of page.

## Out of scope

- Paginating the inactive-SKUs list.
- A per-page preference persisted server-side or in localStorage (URL param only).
- Client-side (JS) pagination / infinite scroll.
- CSV pagination (the export stays the full Overview velocity CSV).

## Success criteria

1. The SKUs tab shows a 10/25/50/100 size selector (default 25) and a Prev/numbers/
   Next pager that walk the full SKU list, preserving sort + period + show_inactive.
2. Insights, "%", and totals remain whole-set; only the table is paged.
3. Invalid/out-of-range params degrade safely; Overview + full suite unchanged.
