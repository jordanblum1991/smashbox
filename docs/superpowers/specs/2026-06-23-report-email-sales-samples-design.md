# Email the Sales & Sample Reports — Design

**Date:** 2026-06-23
**Status:** Approved (design)

## Context

The **inventory report** already emails recipients an HTML body + a formatted
attachment, with a manual "send now" button and a weekly cron — backed by
`app/services/inventory_report_email.py`, dedicated `Shop` columns
(`inventory_report_enabled/hour/minute/days/recipients`), three routes in
`app/routers/reports.py`, and an APScheduler job in `app/services/scheduler.py`.

We want the same for the **Sales report** (`/reports/sales`) and the **Sample
report** (`/reports/samples`): email recipients the report's **HTML summary + a
CSV**, with a manual button and a configurable recurring schedule. Both reports
already have CSV exports (`/reports/sales.csv`; `/samples-by-sku.csv`) and are
**period-scoped** (unlike inventory's snapshot), which drives the period handling
below.

## Decisions (from brainstorming)

- **Both** a manual "Email report" button **and** a recurring schedule per report —
  full parity with inventory.
- **Separate recipient list per report** (Sales has its own, Samples its own).
- **Period:**
  - Manual button → the **on-screen scope** currently selected on the page.
  - Scheduled send → a **configurable rolling window**, recomputed at each fire.
- **Rolling-window options differ per report** (Sales is day-capable; Samples is
  month-granular):
  - **Sales:** `prev_month` · `mtd` · `prev_week` · `last_7` · `last_30` ·
    `prev_fiscal_month`. (Sales handles `prev_fiscal_month` via its native fiscal
    scope — `granularity=fiscal_month`, year/month of the previous fiscal month.)
  - **Samples:** `prev_month` · `mtd` only. The fiscal month is a **29th–28th**
    window (not calendar-aligned) and the Samples report is calendar-month-granular,
    so `prev_fiscal_month` and day-level windows are **not** offered for Samples.
    (Manual "Email report" still covers whatever scope is on the Samples page.)
- **Config UI:** per-report settings card on each report page (mirrors inventory).
- CSV (not XLSX), since these reports' existing exports are CSV.
- **The HTML body and the CSV in a given email MUST represent the same dataset.**
  Each email is built from **one** rows source per report; the HTML table is the
  styled rendering of exactly those rows and the CSV is the data rendering of exactly
  those rows. They cannot diverge because they share one source + one totals
  computation.

## Architecture

### 0. Shared helper — `app/services/report_email_common.py` (new)

Factor the genuinely-shared *new* logic (inventory left untouched — it is in prod):

- **Inline-CSS style constants** for email bodies (the `_CARD/_TH/_TD/_TOT…`
  constants currently inside `inventory_report_email.py`). Lift a copy here and have
  the two new services import them. (Inventory keeps its own copy; do **not** refactor
  inventory to import these — out of scope, avoids touching prod code.)
- **`ROLLING_PERIODS`** — ordered `(key, label)` definitions, with a per-report
  allow-list (`SALES_PERIODS`, `SAMPLE_PERIODS`).
- **`resolve_rolling_period(key, *, today) -> RollingWindow`** where
  `RollingWindow` is a small dataclass `(start: date, end: date, label: str)`
  (`end` inclusive, calendar dates). Mapping (all relative to `today`, shop-local):
  - `prev_month` → first..last day of the previous calendar month.
  - `mtd` → first of the current month .. `today`.
  - `prev_week` → previous Mon–Sun week.
  - `last_7` → `today-6 .. today`; `last_30` → `today-29 .. today`.
  - `prev_fiscal_month` → the previous fiscal month (via `current_fiscal_ym`),
    expressed as its enclosing calendar `start..end`.
  Unknown key → `prev_month` (safe default).
- **`register_report_job(scheduler, job_id, *, enabled, days, hour, minute, run_fn,
  timezone)`** — a generic APScheduler add/replace/remove helper the two new jobs
  call (so the two `apply_*_report_schedule` functions stay 3 lines each). Inventory's
  existing `apply_inventory_report_schedule` is left as-is.

### 1. `Shop` model + migration

Add **6 columns per report** (12 total), mirroring the inventory shape plus the new
`period` column:

```
sales_report_enabled    Boolean  default False
sales_report_hour       Integer  default 8
sales_report_minute     Integer  default 0
sales_report_days       String   default "mon"
sales_report_recipients String(1024) default ""
sales_report_period     String(32)   default "prev_month"

sample_report_enabled    Boolean  default False
sample_report_hour       Integer  default 8
sample_report_minute     Integer  default 0
sample_report_days       String   default "mon"
sample_report_recipients String(1024) default ""
sample_report_period     String(32)   default "prev_month"
```

Plus two properties: `sales_report_recipients_list` and
`sample_report_recipients_list` (parse the comma-separated column, like the existing
`report_recipients_list`). **One Alembic revision** adds all 12 columns (new columns
must go through Alembic now, per the Postgres cutover). Defaults keep both reports
**off + empty** until configured.

### 2. Email services

**HTML/CSV must match (load-bearing):** for each report the HTML table and the CSV are
rendered from the **same rows list** with the **same totals**. The render function and
the CSV builder both take that one rows list — there is no second dataset, so the email's
HTML and its attachment are identical data in two formats.

**`app/services/sales_report_email.py`** (new):
- Dataset = `view.buckets` (the velocity rows). Both the HTML table and the CSV are this
  list; the KPI header figures (Total Revenue/Units/Orders/AOV/Avg-daily) come from the
  same `view` totals, so the summary, table, and CSV all agree.
- `render_sales_email(view, *, window_label) -> (subject, html, text)` — inline-styled
  HTML: header (title + period), the summary KPIs, and the velocity table
  (Period · Start · Revenue · Units · Orders · AOV · In Progress) — **the same columns
  and rows as the CSV**. Plain-text parallel.
- `build_sales_csv(view) -> bytes` — the **extracted** sales velocity CSV (header +
  one row per `view.buckets`, the exact columns of `/reports/sales.csv`). The existing
  `sales_csv` route is refactored to call this builder (DRY; one source).
- `send_sales_report(db, *, recipients, granularity, start_date, end_date, year,
  month) -> None` — resolves `view` via the same `_sales_view_data` scope path, renders
  + attaches the CSV (both from that one `view`), sends via `mailer.send_email`. Raises
  `ValueError` on empty recipients.

**`app/services/sample_report_email.py`** (new):
- Dataset = the **samples-by-SKU rows** (`samples_by_sku_shipped(db, start, end)`).
  This is the SAME data the CSV already contains, so the email's HTML table renders
  these rows (NOT the page's separate `SampleView` summary, which would diverge from
  the CSV). Totals (samples sent, units sold, etc.) are summed from these rows.
- `render_sample_email(rows, *, title_suffix) -> (subject, html, text)` — inline-styled
  HTML: header (title + period `title_suffix`), a totals line summed from `rows`, and
  the by-SKU table (SKU · Product · Samples Sent · Orders Shipped · Units Sold ·
  Sold/Sample) — **the same columns and rows as the CSV**.
- `build_sample_csv(rows) -> bytes` — the **extracted** samples-by-SKU CSV (same
  columns as `/samples-by-sku.csv`). The existing `export_samples_by_sku_csv` route is
  refactored to call this builder.
- `send_sample_report(db, *, recipients, period, year, month, start_year, start_month,
  end_year, end_month) -> None` — resolves the window (the `compute_pnl_view` +
  `window_for` path the existing CSV uses, so the period title matches), pulls the
  by-SKU `rows` once, renders + attaches the CSV (both from that one `rows` list), sends.

Both reuse `mailer.send_email(subject, text, to=recipients, html=html,
attachments=[(filename, csv_bytes, "csv")])`. (`mailer` already supports an arbitrary
MIME subtype; `"csv"` → `text/csv`.)

### 3. Routes — `app/routers/reports.py`

Per report, mirroring the inventory trio (Shop fetched via
`db.query(Shop).order_by(Shop.id).first()`):

- **Settings card** rendered on the existing report page (no new GET route — extend
  the existing `samples_view` / `sales_view` context with the shop's email settings +
  a `sent`/`err` flash query param).
- **`POST /reports/sales/email-settings`** and **`POST /reports/samples/email-settings`**
  — form fields: `recipients`, `enabled`, `period`, `days[]`, `hour`, `minute`. Parse
  + validate (period must be in that report's allow-list, else its default), persist to
  the Shop columns, set `enabled = bool(enabled and chosen days and recipients)`, call
  `apply_*_report_schedule(shop)`, redirect back.
- **`POST /reports/sales/send-now`** and **`POST /reports/samples/send-now`** — email
  immediately to the saved recipients, covering the **current on-screen scope** (the
  form posts the page's active scope params: sales → granularity/start_date/end_date/
  year/month; samples → period/year/month/range). On empty recipients → redirect
  `?err=no-recipients`; on send failure → `?err=send-failed`; success → `?sent=ok`.

### 4. Scheduler — `app/services/scheduler.py`

Add, parallel to the inventory job:
- Job IDs `SALES_REPORT_JOB_ID`, `SAMPLE_REPORT_JOB_ID`.
- `_run_sales_report_job()` / `_run_sample_report_job()` — open a session, load the
  shop, bail if disabled or no recipients, **resolve the configured rolling period to a
  scope**, call the matching `send_*_report(...)`, and on exception route through the
  existing **sync-failure alert** path (same as the inventory job) so a failed
  scheduled send is surfaced, not silent.
- `apply_sales_report_schedule(shop)` / `apply_sample_report_schedule(shop)` — thin
  wrappers over `register_report_job(...)`. Registered at boot for the existing shop,
  same as inventory.
- Honors the existing `SCHEDULER_ENABLED` gate (off in dev/tests), so no cron side
  effects in the suite.

### 5. Templates

- A reusable **`partials/report_email_settings.html`** macro (recipients textarea,
  on/off, the rolling-period `<select>` scoped to the report's options, day checkboxes,
  hour/minute, Save button, and the "Email report" send-now button carrying the current
  scope as hidden fields) — parameterized by the report's POST action + current values.
  Mirrors the inventory page's settings card.
- Included on `reports/sales.html` (in the Overview tab / page header area) and
  `reports/sample_tracking.html`, with the `?sent=ok` / `?err=…` flash.

## Data flow

```
Manual:  page (on-screen scope) → POST /reports/<r>/send-now (scope in form)
          → send_<r>_report(db, recipients, <scope>) → mailer.send_email(html + CSV)
Scheduled: cron fires _run_<r>_report_job → load shop → resolve_rolling_period(period)
          → send_<r>_report(db, recipients, <resolved scope>) → mailer.send_email
Settings: POST /reports/<r>/email-settings → persist Shop cols → apply_<r>_schedule
```

## Error handling / edge cases

- **Empty recipients** → `send_*` raises `ValueError`; button → `?err=no-recipients`;
  schedule won't register (mirrors inventory's `apply_*` guard).
- **Send failure** (SMTP) → button surfaces `?err=send-failed`; scheduled job logs +
  fires the sync-failure alert; never silently swallowed.
- **Empty period data** (no orders/samples in the window) → a valid email with a
  zero/empty table still sends (the report renders an empty state); not an error.
- **Invalid `period` key** → falls back to that report's default (`prev_month`).
- **Samples month-granularity** → only month-level rolling options are offered for
  samples; the resolver returns month-aligned windows for them.
- **SMTP not configured** (no Fly SMTP secrets, e.g. local) → `mailer.send_email`
  behaves as it does today for the inventory/alert emails (its existing no-config
  handling applies; not re-specified here).
- **`SCHEDULER_ENABLED=false`** → schedules are no-ops in dev/tests.

## Testing

Services (`tests/test_sales_report_email.py`, `tests/test_sample_report_email.py`):
- `render_*` returns a subject containing the period label and an HTML body containing
  the KPI/total figures + table rows (assert on seeded values).
- `build_*_csv` returns bytes whose header + a known data row match the existing export
  columns (and equals what the existing route now produces, since both share the
  builder).
- **HTML↔CSV parity:** seed data, then assert the rendered HTML body and the CSV are
  built from the same rows — every data row (SKU code / period label) and the totals in
  the HTML appear in the CSV and vice-versa (same count of rows, same totals). This is
  the test that enforces "the HTML report matches the CSV in the same email."
- `send_*_report` with a **fake mailer** (monkeypatch `mailer.send_email`) is called
  once with the right recipients, an HTML body, and exactly one `.csv` attachment;
  empty recipients → `ValueError`.

Rolling period (`tests/test_report_rolling_period.py`):
- `resolve_rolling_period(key, today=<fixed date>)` returns the correct
  `(start, end, label)` for each key (prev_month across a year boundary; mtd; prev_week;
  last_7/last_30; prev_fiscal_month) — fixed `today` so it's deterministic.
- unknown key → `prev_month`.

CSV-builder parity (extend existing route tests if present, else add): the refactored
`/reports/sales.csv` and `/samples-by-sku.csv` still return the same bytes as before
(guards the extraction).

Routes (`tests/test_report_email_routes.py`):
- `POST …/email-settings` persists recipients/period/schedule to the Shop and flips
  `enabled` only when recipients + days present; invalid period → default.
- `POST …/send-now` with recipients (fake mailer) → `?sent=ok` and the mailer called
  with the on-screen scope; no recipients → `?err=no-recipients`.
- The settings card renders on each report page.

Migration: `tests/test_migrations.py` parity must stay green (the 12 new columns appear
in both the model and the new revision).

## Out of scope (deliberately)

- PDF attachments; changing the inventory feature; per-recipient personalization.
- A central multi-schedule "Scheduled Emails" screen (chose per-report settings).
- Absolute fixed-date scheduled sends (rolling windows only).
- Day-level or fiscal rolling windows for the month-granular Samples report.
- Emailing specific Sales tabs (SKUs/Timing/Heatmap) — the email covers the
  Overview-style summary + the velocity CSV.

## Success criteria

1. On both the Sales and Sample report pages: a per-report email-settings card
   (recipients · on/off · rolling period · days · time) **and** an "Email report" button
   that sends the on-screen scope to the saved recipients.
2. A recurring scheduled send per report that, when it fires, emails the configured
   **rolling window** (recomputed each time) — separate recipients per report.
3. Each email carries the report's **HTML summary + a CSV attachment built from the
   same dataset** — the HTML table and the CSV always match (same rows + totals); the
   on-screen CSV exports are unchanged (now sharing the extracted builder).
4. Failures (no recipients / SMTP error) surface; nothing sends until configured;
   inventory + the full suite stay green.
