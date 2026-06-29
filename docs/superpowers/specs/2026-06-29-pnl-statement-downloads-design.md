# P&L statement downloads — per-fiscal-month CSV + PDF

**Date:** 2026-06-29
**Status:** Approved — implementing

## Goal
A page listing every fiscal month, each with a **CSV** and **PDF** download of that
month's P&L. Fiscal months only (29th→28th, labeled by closing month). No
YTD/year rollups, no email/scheduling. The existing `.xlsx` exports stay as-is.

## What already exists (reuse)
- `compute_pnl_view(db, PeriodKind.FISCAL_MONTH, year, month)` → `PnLView.total`
  (a `MonthlyPnL`) for the fiscal window. The displayed/`xlsx` numbers use the
  **managed** P&L (`managed_net_customer_sales`, `managed_gross_profit`,
  `managed_net_profit`, `managed_gross_margin`, `managed_net_margin`) so the
  Smashbox-funded-discount reimbursement is offset. All formats must match.
- `app/reports/fiscal_calendar.py` — fiscal windows, labels, range strings.
- WeasyPrint PDF pattern: `app/services/invoice_pdf.py` renders a plain-CSS Jinja
  template → PDF (lazy import; system libs present in the Docker image, may be
  absent on a dev box).
- The `/export/monthly-pnl.xlsx` line list is the canonical waterfall to mirror.

## Design

### 1. Shared statement lines — `app/reports/pnl_statement.py`
`statement_lines(pnl: MonthlyPnL) -> list[StatementLine]` — the single source of
the P&L waterfall (label, amount, kind ∈ {line, deduction, subtotal, note}) so
CSV and PDF render identical numbers. Mirrors the xlsx lines: Gross Sales, GMV,
the three discount lines + the Smashbox-funded contra note, Net Customer Sales,
COGS, Gross Profit, itemized TikTok fees, ads ± reimbursement/credits, shipping
×3, adjustments, Net Profit, plus Gross/Net margin.

### 2. Available months — `app/reports/pnl_statement.py`
`available_fiscal_months(db) -> list[FiscalMonthRef]` — fiscal months from the
earliest `Order.placed_at`'s fiscal month through the current fiscal month,
newest-first. Each ref: `year, month, label ("Fiscal May 2026"), range
("Apr 29 – May 28, 2026")`. Needs a new pure helper
`fiscal_calendar.fiscal_ym_for(d: date) -> (year, month)` (date → its fiscal
month: day ≤ 28 → that month; day ≥ 29 → next month, rolling the year).

### 3. The page — `GET /reports/pnl/downloads`
New route in `app/routers/reports.py` + template `reports/pnl_downloads.html`.
Title "P&L Statements". One row per fiscal month: label, date range, and **CSV**
+ **PDF** buttons linking to the exports below. Empty-state when there's no data.
Linked from the P&L page header ("Download statements").

### 4. CSV — `GET /export/pnl.csv?period=fiscal_month&year=Y&month=M`
New route in `app/routers/exports.py`. Reuses `compute_pnl_view` + `statement_lines`.
Header rows: report title, fiscal label, range. Then `Line Item,Amount` rows
(amounts as plain decimals; deductions negative, matching the xlsx signing).
Filename `smashbox_pnl_fiscal_2026-05.csv`. Streamed like the other CSV exports.

### 5. PDF — `GET /export/pnl.pdf?period=fiscal_month&year=Y&month=M`
New `app/services/pnl_pdf.py` (`render_pnl_pdf(view, request) -> bytes`, lazy
WeasyPrint import, same as invoices) + plain-CSS `reports/pnl_pdf.html`: a clean
one-page statement — title, fiscal range, the waterfall with subtotals + margins.
Filename `smashbox_pnl_fiscal_2026-05.pdf`, `application/pdf`.

Both export routes accept the generic period params (default `fiscal_month`) so
they could serve other periods later, but the page only links fiscal months.

## Tests (TDD)
- `fiscal_ym_for`: day 28 → that month; day 29 → next month; Dec 29 → next Jan.
- `available_fiscal_months`: earliest-order month … current, newest-first; empty
  DB → empty list.
- `statement_lines`: a seeded fiscal month yields the expected labels + amounts,
  and Net Profit equals `pnl.managed_net_profit`.
- `/export/pnl.csv`: 200, `text/csv`, contains the line labels + a known amount.
- `/export/pnl.pdf`: 200 `application/pdf` — **skipped** if WeasyPrint's system
  libs aren't importable locally (mirrors the invoice-PDF test).
- `/reports/pnl/downloads`: 200, lists the seeded fiscal month with CSV + PDF links.

## Scope boundaries
Fiscal months only; one file per month; managed P&L numbers; no YTD/year, no
email. `xlsx` export untouched.
