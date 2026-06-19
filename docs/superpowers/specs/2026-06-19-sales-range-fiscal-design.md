# Sales Report — Custom Date Range + Fiscal Scopes — Design

**Date:** 2026-06-19
**Status:** Approved (design)

## Problem

The shipped Sales velocity report (`/reports/sales`) supports only three fixed
trailing windows: Daily (30d), Weekly (12wk), Monthly (12mo). Users want (1) an
arbitrary **custom date range**, and (2) a **fiscal** view aligned to the
company's fiscal months (29th→28th, labeled by the closing month) — the same
fiscal scopes the P&L and Ad-Spend reports already offer.

Both are time-window features, so they extend `compute_sales_report`'s windowing
and the page's control bar. The revenue formula is unchanged (still ties to
`MonthlyPnL.gmv`).

## Scope decisions (from brainstorming)

- **Date range** works *with* the granularity toggle: pick any start/end, bucket
  that span by the active Daily/Weekly/Monthly granularity. Clear → back to the
  default trailing window.
- **Fiscal** mirrors the P&L / Ad-Spend `Fiscal ▾` dropdown: **Fiscal month /
  Fiscal YTD / Fiscal year**.
  - **Fiscal month** → **daily** bars across that fiscal month's 29th–28th window
    (drill into one fiscal month by day).
  - **Fiscal YTD** → **fiscal-month** bars (fiscal Jan … selected month).
  - **Fiscal year** → **fiscal-month** bars (all 12 fiscal months of the year).
- Date range and fiscal are mutually exclusive paths: fiscal uses a year/month
  selector and the `fiscal_calendar` windows, not arbitrary dates.

## Reused building blocks

- `app/reports/fiscal_calendar.py`: `fiscal_window(year, month) -> (start,
  end_incl)`, `fiscal_months_for(mode, month) -> list[int]`, `fiscal_label`,
  `fiscal_banner_payload(period_value, year, month)`. Fiscal month = 29th→28th,
  labeled by closing month; the non-leap-Feb edge is already handled there.
- The existing daily/weekly/monthly bucketing in `compute_sales_report`
  (`_window_for`, `_bucket_start`, `_span_starts`, `_label`, `_key`) — reused
  verbatim for the calendar scopes and for Fiscal-month's daily bars.
- The Ad-Spend report (`compute_ad_spend_fiscal`, and its template's tidy
  `Fiscal ▾` + date-range control bar) is the closest precedent — mirror its
  URL/scope contract and control layout.

## Architecture

### 1. `compute_sales_report` extension (`app/reports/sales_report.py`)

New signature:
```python
def compute_sales_report(
    db, granularity="daily", *,
    start: date | None = None, end: date | None = None,
    fiscal_year: int | None = None, fiscal_month: int | None = None,
    as_of: date | None = None,
) -> SalesReportView
```

`GRANULARITIES` stays `("daily", "weekly", "monthly")` for the calendar toggle; a
new constant `FISCAL_MODES = ("fiscal_month", "fiscal_ytd", "fiscal_year")` covers
the fiscal scopes. Dispatch:

- **Calendar (`daily`/`weekly`/`monthly`)** — window is `[start, end]` when both
  are supplied (custom range), else the existing trailing window from
  `_window_for`. Everything downstream (bucket seeding, aggregation, deltas,
  peak, in-progress flag) is unchanged.
- **`fiscal_month`** — compute `start, end_incl = fiscal_window(fiscal_year,
  fiscal_month)` and render **daily** buckets across `[start, end_incl]` — i.e.
  the daily path with that window. `view.granularity` stays a fiscal value for
  the template/CSV, but the buckets are days.
- **`fiscal_ytd` / `fiscal_year`** — enumerate `fiscal_months_for(mode,
  fiscal_month)` for `fiscal_year`; for each fiscal month `mm`, compute its
  `fiscal_window` and sum PAID-order revenue/units/orders over that window
  (bucketed by `placed_local_date`), producing **one bucket per fiscal month**.
  Bucket `key = f"F{fiscal_year}-{mm:02d}"`, `label = fiscal_label(...)` (e.g.
  "May 2026"). The fiscal month containing today is flagged in-progress; the
  trend delta excludes it, exactly as the calendar path does.

A small internal refactor extracts the per-window "sum PAID orders into a bucket"
core so the calendar daily/weekly/monthly path and the fiscal-month path share
one summation (DRY; guards against the two diverging). The revenue formula is
untouched.

Defaults: `fiscal_year`/`fiscal_month` default to the fiscal month containing
`as_of` (today) when omitted. An unknown granularity falls back to `daily`
(existing guard).

### 2. Route (`app/routers/reports.py`)

`sales_view(request, granularity="daily", start_date=None, end_date=None,
year=None, month=None, db=...)`:
- Calendar scope: parse `start_date`/`end_date` (ISO) if both present → pass as
  `start`/`end`; a malformed or start>end range renders an inline error and the
  default window (mirrors the Ad-Spend date-range error handling).
- Fiscal scope (`granularity in FISCAL_MODES`): resolve `year`/`month` (default to
  the current fiscal month), pass `fiscal_year`/`fiscal_month`, and add
  `fiscal_banner = fiscal_banner_payload(granularity, year, month)` to the
  context.
- Context also carries the current scope + range values so the controls
  round-trip (the form re-shows what was applied).

`sales_csv(...)` takes the same params and dispatches identically, so the export
matches whatever scope is on screen. Filename encodes the scope (e.g.
`sales_fiscal_year_2026.csv`, `sales_2026-03-01_to_2026-06-15.csv`).

### 3. Template (`app/templates/reports/sales.html`)

Mirror the Ad-Spend control bar, kept tidy:
- The existing **Daily / Weekly / Monthly** segmented toggle.
- A **Fiscal ▾** dropdown (Fiscal month / Fiscal YTD / Fiscal year), each link
  carrying `?granularity=fiscal_*&year=&month=`.
- A compact **date-range form** (two native `<input type=date>` + **Apply**, and
  a **Clear** link to the default window), shown for the calendar scopes and
  carrying the active granularity. An inline error message slot for a bad range.
- The shared **fiscal accent banner** (`fiscal_banner`) naming the window, shown
  only for fiscal scopes — same partial/markup the P&L + Ad-Spend use.
- The chart (with the new x-axis labels), summary cards, velocity table, and
  Download-CSV button already render off `view.buckets` + `view.granularity`, so
  they need no per-scope changes. The Download-CSV href carries the active scope's
  params.

### Data flow

```
URL scope (granularity [+ start/end | + year/month])
  → compute_sales_report(... window resolved per scope ...)
  → one SalesReportView (buckets vary by scope)
  → render sales.html  /  stream sales.csv
```

## Error handling / edge cases

- Custom range with start > end, or unparseable dates → inline error + default
  window, HTTP 200 (no 500).
- Fiscal with missing year/month → default to the current fiscal month.
- Empty data in any window → zero buckets still render (existing behavior).
- Fiscal-month daily window spanning a month boundary (Apr 29–May 28) — handled
  by `fiscal_window`; daily buckets cross the calendar boundary correctly because
  bucketing is by `placed_local_date`, not calendar month.
- In-progress flag: the current calendar day (daily/range/fiscal-month) or the
  current fiscal month (fiscal-ytd/year) is flagged and excluded from the delta.

## Testing

No network; SQLite; seed PAID orders. New/extended tests:

- **Custom range:** `start`/`end` produce buckets only within the span; weekly and
  monthly bucket a custom range correctly; `start > end` → route renders the error
  + default window.
- **Fiscal month → daily:** `fiscal_month` for fiscal May 2026 yields daily buckets
  spanning **Apr 29 – May 28**; an order on Apr 29 and one on May 28 both land in
  the window, an order on May 29 does not.
- **Fiscal YTD / year → fiscal-month buckets:** correct bucket counts (ytd = Jan..M,
  year = 12) and labels; a fiscal-May bucket's revenue equals the revenue over
  `fiscal_window(2026, 5)`.
- **Parity:** a fiscal-month sales bucket equals the P&L's GMV for the same fiscal
  window (`compute_pnl_view`/`compute_ad_spend_fiscal` agreement), locking the
  "ties to the dashboard" guarantee across fiscal too.
- **Route + CSV:** each scope returns 200 with the right heading; the fiscal banner
  is present for fiscal scopes and absent for calendar scopes; CSV exports per
  scope with a scope-encoded filename.

## Out of scope (YAGNI)

- Fiscal *weekly* buckets (fiscal is month-grained; Fiscal month drills to daily).
- Saved/bookmarked ranges beyond the URL query string.
- A custom-range variant of the fiscal scopes (fiscal uses its own year/month
  window).

## Success criteria

1. A date-range form lets you pick any start/end and see it bucketed by the active
   Daily/Weekly/Monthly granularity; Clear returns to the default window.
2. A Fiscal ▾ dropdown offers Fiscal month / YTD / year; Fiscal month shows daily
   bars across the 29th–28th window, YTD/year show fiscal-month bars; the fiscal
   accent banner names the window.
3. Fiscal sales buckets tie to the P&L fiscal view for the same window.
4. CSV export matches the on-screen scope. Full suite green.
