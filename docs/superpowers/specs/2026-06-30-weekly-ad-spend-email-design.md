# Weekly Ad-Spend Email — Design

**Date:** 2026-06-30
**Status:** Approved (design); ready for implementation plan
**Author:** Claude (with jordanblum1991)

## Summary

Add a **weekly emailable Ad-Spend report**, mirroring the existing weekly inventory /
sales / sample emails. Each email pairs a **budget tracker** (gross spend since the
current budget period's start, and budget remaining for the year) with a **per-day
spend table for the previous complete week (Mon–Sun)**.

The report reuses the existing **AdBudget** system (`/admin/ad-budget`,
`app/reports/ad_budget.py`) and the existing **emailed-report machinery**
(`report_email_common`, the shared settings-card partial, the scheduler's
`register_report_job` helper, and `mailer.send_email`). No new budget model.

## Motivation

The ad budget for **Jul 1, 2026 → Jun 30, 2027** is **$35,000** (shown on
`/admin/ad-budget`). The operator wants a weekly email that answers:

1. How much have we spent against the budget since July 1?
2. How much budget is left for the year?
3. What did we spend each day last week?

Because the budget amount is a **live, editable field**, any mid-period top-ups
(e.g. raising $35,000 → $45,000) are reflected automatically — the email reads the
current budget at send time. No additive-ledger feature is required.

## Out of scope (YAGNI)

- A configurable reporting-window dropdown for the email. The window is fixed:
  daily table = previous complete week (Mon–Sun); budget block = the current budget
  period. (The other emails expose a rolling-period selector; this one does not need
  one.)
- Per-campaign breakdown, net-after-credits per day, Shop Ads spend. The budget
  engine and this email both track **GMV-Max** spend only (`GmvMaxDailyMetric.cost`).
- Any change to the AdBudget model or the `/admin/ad-budget` page.

## Architecture

Two independent windows, by design:

- **Daily table window** — the previous complete week, Monday–Sunday, relative to the
  send date (`today_local()`).
- **Budget window** — always the **current** `AdBudget` (the one whose
  `[start_date, end_date]` contains today), independent of the weekly window.

Data flows: `compute_ad_spend_email_view` composes the two existing engines into one
`AdSpendEmailView`; the render/CSV/send functions consume only that view; the
scheduler and routes drive `send_ad_spend_report`.

### 1. Data layer — `app/reports/ad_spend_email.py`

```python
@dataclass
class AdSpendEmailDay:
    day: date
    gross_spend: Decimal

@dataclass
class AdSpendEmailView:
    week_start: date
    week_end: date
    days: list[AdSpendEmailDay]      # exactly 7, zero-filled, Mon..Sun
    week_total: Decimal

    has_budget: bool
    budget_label: str | None
    budget_start: date | None
    budget_end: date | None
    budget_amount: Decimal           # 0 when has_budget is False
    spend_since_start: Decimal       # AdBudgetView.total_ad_spend
    remaining: Decimal               # AdBudgetView.available
    pct_used: Decimal
    is_over_budget: bool
    days_remaining: int
    projected_total: Decimal


def compute_ad_spend_email_view(
    db: Session, *, week_start: date, week_end: date, today: date | None = None
) -> AdSpendEmailView: ...
```

Behavior:

- **Week table:** sum `GmvMaxDailyMetric.cost` per day over `[week_start, week_end]`
  and zero-fill to a full 7-row Mon–Sun grid; `week_total` is their sum.
- **Source consistency:** the budget engine sums `GmvMaxDailyMetric.cost`. The week
  table reads the **same** source so the week total ties to the budget's spend basis.
  During implementation, confirm `compute_ad_spend_daily` uses that source; if it
  diverges, query `GmvMaxDailyMetric` directly for the grid.
- **Budget block:** `b = current_budget(db, today=today)`. If present,
  `bv = compute_budget_view(db, b, today=today)` and copy `budget_amount`,
  `total_ad_spend`→`spend_since_start`, `available`→`remaining`, `pct_used`,
  `is_over_budget`, `days_remaining`, `projected_total`, plus `label`/`start_date`/
  `end_date`. If no budget covers today, `has_budget=False` and budget fields take
  zero/None defaults.

### 2. Email content — `app/services/ad_spend_report_email.py`

Three functions (same shape as the other report emails):

- `render_ad_spend_email(view) -> tuple[str, str, str]` → `(subject, html, text)`
  - Subject: `Smashbox Ad Spend — week of <week_start %b %d>`.
  - HTML (inline-styled via `report_email_common` constants):
    - **Budget block** (card): Allocated · Spent since `<budget_start>` · **Remaining**
      · % used · Projected at pace · Days left. Renders red when `is_over_budget`.
    - **This week (Mon–Sun)** table: 7 rows `Day | Gross spend`, bold **Week total**.
    - When `has_budget=False`: budget block replaced by a muted
      "No active ad budget covers this week" note; week table still renders.
  - `text`: plain fallback with the same figures.
- `build_ad_spend_csv(view) -> bytes`
  - Budget summary block (label, period, allocated, spent-since-start, remaining,
    % used, projected, days left), then a `Day,Gross spend` section for the 7 days and
    a total line. Same numbers as the HTML.
- `send_ad_spend_report(db, *, recipients, today=None) -> None`
  - Resolve the previous complete week from `today` (default `today_local()`), build
    the view, render, and call
    `mailer.send_email(subject, text, to=recipients, html=html,
    attachments=[(filename, csv_bytes, "csv")])`.
  - Raises `ValueError` on empty recipients.

### 3. Config + schedule

**Shop columns** (new Alembic revision; 5 fields — no `period`, window is fixed):

| column | type | default |
|---|---|---|
| `ad_spend_report_enabled` | bool | `False` |
| `ad_spend_report_hour` | int | `8` |
| `ad_spend_report_minute` | int | `0` |
| `ad_spend_report_days` | str(64) | `"mon"` |
| `ad_spend_report_recipients` | str(1024) | `""` |

Plus a `ad_spend_report_recipients_list` property (comma-split, stripped, non-empty).
`tests/test_migrations.py` model↔migration parity must stay green.

**Scheduler** (`app/services/scheduler.py`):

- `AD_SPEND_REPORT_JOB_ID = "ad_spend_report_email"`.
- `_run_ad_spend_report_job()` — own `SessionLocal()`; resolve primary shop; skip
  (log) if disabled or no recipients; compute the previous complete week; call
  `send_ad_spend_report`; on exception, log and fire the existing report-failure alert.
  Never propagates.
- `apply_ad_spend_report_schedule(shop)` — register/reschedule/remove via the shared
  `register_report_job(...)` helper using the shop's day/hour/minute/timezone.
- Call `apply_ad_spend_report_schedule(shop)` in `start_scheduler()` alongside the
  other report schedules. Gated by `SCHEDULER_ENABLED`.

**Routes** (`app/routers/reports.py`):

- Extend `GET /reports/ad-spend` context with `shop`, `valid_days`,
  `smtp_configured`, and flash flags.
- `POST /reports/ad-spend/email-settings` (admin) — validate time, normalize days +
  recipients, set `enabled` only when both days and recipients are present, commit,
  `apply_ad_spend_report_schedule`, redirect with a flash.
- `POST /reports/ad-spend/send-now` (admin) — call `send_ad_spend_report` for the
  previous week immediately; redirect `?sent=ok` on success, `?err=no-recipients` /
  `?err=send-failed` otherwise (no traceback surfaced to the user).

**Page** (`app/templates/reports/ad_spend.html`): include the shared
`report_email_settings` settings-card partial (recipients, day-of-week, time, enabled
toggle, "Send now", SMTP-not-configured warning) — identical UX to the inventory /
sales pages.

## Edge cases / error handling

- **No budget covers the week** → email still sends; budget block shows the muted
  note; scheduler does not error.
- **Budget not yet started** (`today < start_date`) → `compute_budget_view` already
  returns spend 0 / full remaining; surfaced as-is.
- **Zero-spend week** → 7 rows of `$0.00`, total `$0.00`.
- **Over budget** → red budget block, negative "Remaining".
- **Empty recipients** → scheduler skips (logs); send-now redirects
  `?err=no-recipients`; `send_ad_spend_report` raises `ValueError`.
- **SMTP unconfigured** → settings card shows the standard warning; send-now redirects
  `?err=send-failed` on failure.

## Testing (TDD) — `tests/test_ad_spend_report_email.py`

- **View:** 7-row zero-fill; `week_total`; budget pull from a seeded `AdBudget` +
  `GmvMaxDailyMetric`; `has_budget=False` path; over-budget path; week total ties to
  the GMV-Max source.
- **Render:** budget figures present; 7 day rows + total row; over-budget styling;
  no-budget note path.
- **CSV:** budget block + daily-section columns; totals line.
- **Send:** monkeypatched mailer asserts recipients, CSV attachment, HTML alternative;
  `ValueError` on empty recipients.
- **Schedule:** `apply_…` registers when enabled + recipients set; removes when
  disabled or recipients empty.
- **Routes:** settings POST persists the 5 fields and reschedules; send-now invokes
  send (monkeypatched); both reject non-admins.

## Files

**New**
- `app/reports/ad_spend_email.py` — composed view.
- `app/services/ad_spend_report_email.py` — render / CSV / send.
- `alembic/versions/<rev>_ad_spend_report_email_settings.py` — 5 Shop columns.
- `tests/test_ad_spend_report_email.py`.

**Changed**
- `app/models/shop.py` — 5 columns + `ad_spend_report_recipients_list` property.
- `app/services/scheduler.py` — job id, `_run_ad_spend_report_job`,
  `apply_ad_spend_report_schedule`, call in `start_scheduler`.
- `app/routers/reports.py` — extend GET context; add the two POST routes.
- `app/templates/reports/ad_spend.html` — include the settings-card partial.

## Reused, not rebuilt

- `app/reports/ad_budget.py` — `current_budget`, `compute_budget_view` (`AdBudgetView`).
- `app/models/ad_budget.py`, `app/models/gmv_max_daily_metric.py`.
- `app/services/report_email_common.py` — styling constants + `register_report_job`.
- `app/templates/partials/report_email_settings.html` — settings-card macro.
- `app/services/mailer.py` — `send_email`.
