# User-editable schedule for the TikTok Marketing (GMV-Max) sync

## Goal

Give the GMV-Max (Marketing API) ad-data sync its own user-editable schedule —
days-of-week + time + on/off — instead of piggybacking on the SAP inventory
weekday job. Editable on `/admin/tiktok-ads`, persisted on `Shop`, live-applied.
Mirrors the existing inventory-sync schedule exactly.

## Current state → change

Today `sync_gmv_max` runs inside `_run_inventory_sync_job` (scheduler.py:88), so
GMV-Max refreshes on the inventory weekday schedule with no independent control.
**Decouple it** into its own scheduled job.

## Shop fields (new — Alembic migration required)

Parallel to `inventory_sync_*`:
- `gmv_sync_enabled: bool` default **True**
- `gmv_sync_hour: int` default **7**
- `gmv_sync_minute: int` default **45**  (offset from inventory's 7:30 so they
  don't fire simultaneously)
- `gmv_sync_days: str` default **"mon,tue,wed,thu,fri,sat,sun"** (daily — the
  ask was for daily; the user can narrow it)

New Alembic revision adds the four columns. Boot `create_all` covers fresh DBs;
the SQLite `_ensure_columns` shim is inert on Postgres, so the migration is the
real path.

## Scheduler (`scheduler.py`)

- Remove the `sync_gmv_max` call from `_run_inventory_sync_job` (inventory job
  goes back to inventory-only).
- Add `_run_gmv_sync_job()` → `sync_gmv_max(db)` + `_run_alert_check(db)`, own
  session, never raises.
- Add `GMV_JOB_ID = "gmv_max_sync"` and `apply_gmv_schedule(shop)` — a copy of
  `apply_inventory_schedule` keyed off `shop.gmv_sync_*` (CronTrigger
  day_of_week/hour/minute in `shop.timezone`; remove the job when disabled).
- Register it in `start_scheduler()` alongside the others.

## Route (`app/routers/tiktok_marketing.py`)

`POST /admin/tiktok-ads/schedule` (admin-only), mirroring
`update_inventory_sync_settings`:
- Parse `enabled` (checkbox), `report_time` "HH:MM", and `days[]` (validated
  against the APScheduler day tokens, week-ordered).
- Persist to `shop.gmv_sync_*`, commit, call `apply_gmv_schedule(shop)`,
  redirect back to `/admin/tiktok-ads` with a notice.
- Bad time / no days → redirect with an error (don't half-save).

## UI (`app/templates/admin/tiktok_marketing.html`)

A schedule card next to the existing "Sync now" button: an enable toggle, seven
day checkboxes, a time input, and Save — same shape as the Uploads inventory
schedule. Echo the current schedule and the GMV-Max last-synced time (reuse the
`last_synced_at(GmvMaxDailyMetric)` value already surfaced on Ad Spend).

## Testing (TDD)

- `apply_gmv_schedule`: enabled → job registered with the shop's day/hour/minute;
  disabled → job removed; safe no-op when scheduler is off.
- Route: valid post persists `gmv_sync_*` + reschedules; bad time → error, no
  save; empty days → error.
- Decoupling: `_run_inventory_sync_job` no longer calls `sync_gmv_max` (guards
  against a double-run); `_run_gmv_sync_job` calls it.
- UI: the schedule form renders on `/admin/tiktok-ads`.
- `tests/test_migrations.py` (models↔migration parity) stays green.

## Out of scope

- Scheduling the TikTok **Shop** sync (orders/settlements/…) — stays daily at the
  env time (separate ask if wanted).
- Multiple sync times per day (single daily time, like the other schedulers).

## Files

- `app/models/shop.py` + new `alembic/versions/*_gmv_sync_schedule.py`
- `app/services/scheduler.py` — decouple + `_run_gmv_sync_job` + `apply_gmv_schedule`
- `app/routers/tiktok_marketing.py` — schedule POST + pass schedule/last-synced to template
- `app/templates/admin/tiktok_marketing.html` — schedule card
- `tests/` — scheduler + route + decoupling + UI
