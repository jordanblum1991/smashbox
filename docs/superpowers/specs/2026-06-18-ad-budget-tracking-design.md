# Ad Budget Tracking Tool — Design Spec

**Date:** 2026-06-18
**Status:** Approved (brainstorming) → ready for implementation plan

## Purpose

Smashbox allocates an ad budget to Outlandish. We need a tool to track how much
of that budget has been spent and how much remains, on a **daily** basis, so we
can answer Smashbox's occasional "what was spent / what's left?" question and
share a report. Budget tracking **starts 2026-07-01**.

## Decisions (from brainstorming)

1. **Spend source — auto.** "What was spent" auto-pulls our already-tracked
   **daily GMV-Max ad spend** (`GmvMaxDailyMetric.cost`). No manual spend entry,
   no double bookkeeping; the running figure updates as daily syncs land.
2. **What counts — GMV-Max only.** Shop Ads (settlement-sourced, ~$869 all-time,
   not daily-granular) is excluded so the daily running number stays exact.
3. **Budget period — flexible date range.** Each budget is `(start_date,
   end_date, amount)`. Handles the undecided monthly-vs-quarterly question: make
   a July budget now (Jul 1–31), switch to a quarterly one (Jul 1–Sep 30) later.
4. **Promotions — dated line items.** A manual carve-out has `(name, amount,
   promo_date)` and reduces available budget **from its date** (a step-down on
   the daily running line), alongside ad spend.
5. **Report — summary + daily detail, exportable.** Headline figures (budget,
   spent, promotions, available) plus a day-by-day running table, with CSV +
   print export to send to Smashbox.

## Architecture

Standalone "Ad Budget" tool, following the app's existing layered pattern
(models / pure-computation report / router / templates), the same shape as the
Invoices/AP feature. Plan (budgets + promotions) is kept separate from actuals
(GMV-Max spend, already tracked).

```
models/ad_budget.py        AdBudget, AdBudgetPromotion (ORM)
reports/ad_budget.py       compute_budget_view (pure; reads GmvMaxDailyMetric)
routers/ad_budget.py       CRUD + report page + CSV export
templates/admin/ad_budget*.html
alembic/versions/...        new-tables migration
```

## Data model — two new tables

### `AdBudget`
| column | type | notes |
|---|---|---|
| id | int PK | |
| label | str(64) | e.g. "July 2026", "Q3 2026" |
| start_date | Date | inclusive; first budget = 2026-07-01 |
| end_date | Date | inclusive |
| amount | Numeric(14,2) | allocated budget, > 0 |
| shop_id | int FK nullable | multi-tenancy pattern (Phase 2a); not query-scoped yet |
| created_at | datetime | naive UTC, per repo convention |

Budgets are independent over their own date ranges. The **current** budget for a
glance is the one whose `[start_date, end_date]` contains today; if several
overlap, pick the most recent `start_date`. Overlap is allowed (not enforced in
DB); each budget is reported independently. `created_at` uses
`datetime.now(UTC).replace(tzinfo=None)` to match the repo's other models.

### `AdBudgetPromotion`
| column | type | notes |
|---|---|---|
| id | int PK | |
| budget_id | int FK → ad_budgets.id, indexed | carve-out from THIS budget |
| name | str(120) | |
| amount | Numeric(14,2) | > 0 |
| promo_date | Date | within the budget's range; reduces available from here |
| note | Text nullable | optional |
| created_at | datetime | |

Relationship: `AdBudget.promotions` (selectinload to avoid N+1 in the report).

### Migration
New tables go through an **Alembic revision** (per CLAUDE.md; boot
`create_all` is belt-and-suspenders only). `tests/test_migrations.py` guards
models↔migration parity, so the revision must match the models exactly.

## Computation — `reports/ad_budget.py` (pure, no writes)

`compute_budget_view(db, budget, *, today=None) -> AdBudgetView`

- **today** defaults to `today_local()` (shop-local). The daily ledger runs from
  `start_date` to `min(end_date, today)` — i.e. actuals through today; future
  days aren't shown (no spend yet).
- **Not-started budget** (`today < start_date`) — relevant now, since budgets
  begin **2026-07-01** but may be created earlier: the ledger is **empty**,
  `total_ad_spend = 0`, `total_promotions = 0`, `available = full budget`,
  `days_elapsed = 0`. The summary flags `not_started = True` so the page can show
  "Starts <date> — full budget available" instead of a $0/day burn rate. All
  `/0` guards (avg daily, % used) return 0 in this state.
- **Daily rows** (`AdBudgetDayRow`), one per calendar day in that window:
  - `day`
  - `ad_spend` — `GmvMaxDailyMetric.cost` for that day in range, else $0
  - `promotions` — sum of promotion `amount` whose `promo_date == day`
  - `committed_to_date` — running sum of (ad_spend + promotions) from start
  - `available` — `budget.amount − committed_to_date`
  - Every day in the window is emitted (including $0-spend days) for a continuous
    running line.
- **Summary** (`AdBudgetView`):
  - `budget_amount`, `total_ad_spend`, `total_promotions`,
    `total_committed = total_ad_spend + total_promotions`,
    `available = budget_amount − total_committed`
  - `pct_used = total_committed / budget_amount` (guard /0)
  - `days_elapsed`, `days_total`, `days_remaining`
  - `avg_daily_spend = total_ad_spend / days_elapsed` (guard /0)
  - `projected_total = avg_daily_spend × days_total + total_promotions`
    (simple burn-rate landing estimate; informational)
  - `is_over_budget = available < 0`
  - `not_started = today < start_date` (see Not-started above)
- Spend is queried with `metric_date >= start_date AND <= min(end_date, today)`,
  so nothing outside the range counts.

Helper: `current_budget(db, *, today=None) -> AdBudget | None` for the list page's
"current period" highlight (and any future dashboard glance).

## Pages & routes — `routers/ad_budget.py`

Nav: new **"Ad Budget"** item near the Ad Spend links.

| route | method | purpose |
|---|---|---|
| `/admin/ad-budget` | GET | list budgets (spent/available per row), current highlighted, "New budget" link |
| `/admin/ad-budget/new` | GET | create form (defaults start to 2026-07-01) |
| `/admin/ad-budget` | POST | create + redirect to its report |
| `/admin/ad-budget/{id}` | GET | **report**: summary cards, daily running table, promotions list + add-form, edit-budget, CSV/Print |
| `/admin/ad-budget/{id}/edit` | POST | edit label/dates/amount |
| `/admin/ad-budget/{id}/promotions` | POST | add a promotion |
| `/admin/ad-budget/{id}/promotions/{pid}/delete` | POST | delete a promotion |
| `/admin/ad-budget/{id}.csv` | GET | export summary + daily ledger as CSV |

Templates: `admin/ad_budget_list.html`, `admin/ad_budget_detail.html` (report),
`admin/ad_budget_new.html`. Print-styled (`print:` classes) so the detail view
PDFs cleanly for sending to Smashbox — no PDF library needed.

**Access:** available to any signed-in user (finance/ops tool), matching how
reports work. (Open question deferred to user: lock budget *entry* to
`require_admin`? Default = not admin-gated.)

## Error handling / edge cases

- Validation (flash error + preserve input, same pattern as Invoices):
  - budget: `label` required, `end_date ≥ start_date`, `amount > 0`
  - promotion: `name` required, `amount > 0`, `promo_date` within
    `[budget.start_date, budget.end_date]`
- No budgets → empty state nudging "create your first budget (starts Jul 1)".
- Over-budget (`available < 0`) and low-budget surface as colored flags, not
  blocks.
- Deleting a budget cascade-deletes its promotions (FK `ondelete=CASCADE` +
  ORM cascade).

## Testing

- `compute_budget_view`: running-available math (budget − spend − promotions,
  cumulative), promotions reduce available **from their date**, spend bounded to
  range (pre-start / post-end excluded), over-budget flag, burn-rate summary,
  `today` clamping (no future rows), and the **not-started** state
  (today < start_date → empty ledger, full budget available, no /0 errors).
- `current_budget` selection (containing today; overlap → latest start).
- Routes: create/edit budget, add/delete promotion, validation errors, CSV
  export content, list + detail render.
- Migration parity (test_migrations).

## Out of scope (YAGNI)

- Dashboard glance tile (can add later if useful).
- Per-channel / per-campaign budgets (single amount per period).
- Including Shop Ads in spend (GMV-Max only by decision).
- Query-level multi-shop scoping (Phase 2b; `shop_id` column added for
  forward-compat only).
- Auto-generating monthly/quarterly periods (flexible manual ranges instead).
