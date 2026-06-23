# Weekly Inventory Report Email — Design Spec

**Date:** 2026-06-23
**Status:** Approved for planning
**Author:** brainstormed with the operator

## Goal

Email a weekly inventory report to a configurable recipient list. Each row
shows, per SKU: the SKU code, product name, **sellable** on-hand, **sample**
on-hand, and **on order** (units on placed-but-unreceived POs). The operator can
manage the recipient list and the send schedule (weekdays + time) from the app,
and trigger an immediate send for testing.

## Key context: most of this already exists

- `app/reports/inventory_report.py::compute_inventory_report(db)` already returns
  an `InventoryReportView` whose rows carry `sku_code`, `name`,
  `sellable_on_hand`, `sample_on_hand`, `total_on_hand`, and `in_transit`
  (= "on order"), plus all totals. **No new inventory computation is needed.**
- `app/services/mailer.py::send_email` is the stdlib-smtplib send seam.
- `app/services/scheduler.py` runs an in-process APScheduler with a per-shop
  weekly cron pattern (`apply_inventory_schedule`) gated by `Shop` fields +
  `settings.scheduler_enabled`. Cron fires in `Shop.timezone`.
- `app/routers/exports.py` already builds an inventory `.xlsx` and a `.csv`.
- `app/services/sync_alerts.py` + `settings.sync_alert_to_list` is the existing
  failure-alert channel.

This feature is **assembly**: format the existing report into an email, add a
recipients + schedule settings panel, and register a scheduler job reusing the
SAP-sync pattern.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Email contents | Inline **HTML table** in the body **+ `.xlsx` attachment** |
| Row scope | **All** SKUs the on-screen report shows (incl. zero-stock + unmapped) |
| Schedule shape | **Multiple weekdays + a time** (same as the SAP sync) |
| Settings home | A panel on the **Inventory Report page** (`/reports/inventory`) |
| Permissions | **Admins only** for "Send now" and settings save (`require_admin`) |
| Scheduled-send failure | **Alert** the operator via the existing sync-alert channel |

## Components

### 1. Data — reuse unchanged
`compute_inventory_report(db)` is the single source. The email lists every row
the page shows.

### 2. New service: `app/services/inventory_report_email.py`
A single focused module with three functions:

- `render_inventory_email(view) -> tuple[str, str, str]`
  Returns `(subject, html_body, text_body)`. The HTML body is a styled table:
  columns **SKU · Product · Sellable · Sample · On Order**, a totals row, and a
  header line ("Smashbox Weekly Inventory — <date>", with last-sync timestamp).
  The text body is a plain fallback for `multipart/alternative`.
- `build_inventory_xlsx(view) -> bytes`
  The `.xlsx` bytes for the attachment. **Refactor:** extract the existing
  inventory Excel-builder logic out of `app/routers/exports.py` into this
  function, and repoint the export route at it, so there is exactly one builder.
- `send_inventory_report(db, *, recipients: list[str]) -> None`
  Computes the view, renders, attaches the `.xlsx`, calls `mailer.send_email`.
  Raises on failure; the caller decides what to do. Raises `ValueError` if
  `recipients` is empty.

### 3. Mailer extension: `app/services/mailer.py`
Extend `send_email` with two optional, backward-compatible params:
`html: str | None = None` and
`attachments: list[tuple[str, bytes, str]] | None = None`
(`(filename, payload, mime_subtype)`). When `html` is set the message becomes
`multipart/alternative` (text + html); attachments are added via
`msg.add_attachment`. Existing sync-alert callers (text-only) are unaffected.

### 4. Storage: `Shop` model + Alembic migration
Add five columns to `shops`, mirroring the SAP-sync fields:

```
inventory_report_enabled     Boolean  default False   # off until configured
inventory_report_days        String   default "mon"   # APScheduler day_of_week
inventory_report_hour        Integer  default 8
inventory_report_minute      Integer  default 0
inventory_report_recipients  String   default ""      # comma-separated
```

A `recipients_list` access is provided by a small helper that parses the
comma-separated string (mirrors `settings.sync_alert_to_list`). A new Alembic
revision adds the columns to Postgres; boot `create_all` covers fresh dev DBs;
`tests/test_migrations.py` parity is maintained.

### 5. Scheduler: `app/services/scheduler.py`
- `REPORT_JOB_ID = "inventory_report_email"`.
- `apply_inventory_report_schedule(shop)` — register/reschedule/remove the cron
  job, mirroring `apply_inventory_schedule`: `CronTrigger(day_of_week=…,
  hour=…, minute=…, timezone=shop.timezone)`, `coalesce=True`,
  `misfire_grace_time=3600`, `max_instances=1`. The job is registered **only
  when `inventory_report_enabled` AND recipients are non-empty**; otherwise it
  is removed.
- `_run_inventory_report_job()` — own DB session; never propagates exceptions.
  On success: `send_inventory_report(db, recipients=…)`. On failure: log, and
  **fire a failure alert** to `settings.sync_alert_to_list` via `mailer`
  (reusing the sync-alert channel) when that channel is configured.
- Registered in `start_scheduler()` next to the inventory/TikTok jobs.

### 6. UI + routes
On `app/templates/reports/inventory_report.html`, add a `print:hidden` settings
card (collapsible) reusing the Uploads-page schedule markup:
recipients input, enabled toggle, weekday checkboxes (`_VALID_DAYS`), time input,
**Save** button, **Send now** button. The card shows an "SMTP not configured"
hint when `settings.smtp_host` is empty.

Two routes (mirroring `update_inventory_sync_settings`), **both
`require_admin`**:
- `POST /reports/inventory/email-settings` — validate time/days/recipients,
  persist on `Shop`, call `apply_inventory_report_schedule(shop)`, redirect back
  with a flash.
- `POST /reports/inventory/send-now` — `send_inventory_report` immediately to the
  saved recipients; redirect back with a success or error flash.

`inventory_report_view` (GET) gains `shop` + flash context so the panel renders
current values.

## Data flow

```
APScheduler cron (shop tz · chosen weekdays · time)
  → _run_inventory_report_job()
    → send_inventory_report(db, recipients=shop.recipients_list)
      → compute_inventory_report(db)
      → render_inventory_email(view)         # subject, html, text
      → build_inventory_xlsx(view)           # attachment bytes
      → mailer.send_email(subject, text, to=recipients,
                          html=html, attachments=[(name, xlsx, "xlsx")])
  (on exception → log + sync-alert email)
```

## Error handling

- **Scheduled send fails:** caught in `_run_inventory_report_job`, logged, and an
  alert email is sent to `sync_alert_to_list` (if configured). Scheduler keeps
  running.
- **Manual send fails:** caught in the route, surfaced as an error flash on the
  page.
- **No recipients:** `apply_inventory_report_schedule` won't register the job;
  `send_inventory_report` raises `ValueError`; the route shows a validation flash.
- **SMTP unconfigured:** the mailer raises; manual send shows the error; the panel
  shows an "SMTP not configured" hint. Saving the schedule is still allowed.

## Testing

1. `render_inventory_email` — table includes a known SKU's sellable/sample/on-
   order cells and a totals row; subject carries the date.
2. `build_inventory_xlsx` — returns non-empty bytes beginning with the XLSX/ZIP
   magic; one row per report row.
3. `send_inventory_report` — monkeypatch the SMTP seam; assert recipients, an
   `.xlsx` attachment, and an HTML alternative are present; empty recipients
   raises `ValueError`.
4. `apply_inventory_report_schedule` — registers the job when enabled + recipients
   set; removes it when disabled or recipients empty.
5. Routes — `POST email-settings` persists the five fields and reschedules;
   `POST send-now` invokes the send (monkeypatched); both reject non-admins.
6. Migration parity (`tests/test_migrations.py`).

## Out of scope (YAGNI)

- Per-recipient or per-SKU customization, multiple report templates.
- Multi-shop scoping (Phase 2b owns query scoping; this reads the single shop).
- Historical archive of sent reports.
- Configurable columns / row filters beyond the locked "all rows" choice.
