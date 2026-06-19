# GMV-Max Auto-Pull — Design

**Date:** 2026-06-19
**Status:** Approved (design)
**Author:** pairing session

## Problem

GMV-Max ad spend, attributed revenue, and order counts are loaded into
`GmvMaxDailyMetric` today by **manually downloading** TikTok's "Campaign overview
(By-Day)" CSV and uploading it. The Ad Spend page (spend, Attributed ROAS,
cost-per-order) reads that table, so the numbers go stale between manual uploads.

TikTok's Marketing API exposes this exact data. We proved it end-to-end against
production credentials: the GMV-Max report endpoint returns daily per-campaign
`cost`, `orders`, and `gross_revenue`, and summed over a month it **ties to the
manually-uploaded CSV to the penny** (May 2026: cost $7,824.02, gross_revenue
$15,769.65 — identical). This spec replaces the manual upload with an automatic
pull, keeping the CSV path as a fallback.

## Proven API recipe (verified on prod, 2026-06-19)

Base: `https://business-api.tiktok.com/open_api/v1.3`

1. **List GMV-Max campaigns** — `GET /gmv_max/campaign/get/` (the dedicated
   GMV-Max listing; the standard `/campaign/get/` does not surface them)
   - `advertiser_id`
   - `filtering = {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX", "LIVE_GMV_MAX"]}`
     (max 2 items; valid `primary_status` ∈ {STATUS_DELETE, STATUS_DELIVERY_OK,
     STATUS_DISABLE})
   - Returns the GMV-Max campaigns; the store id is available from the campaign
     config (`/campaign/gmv_max/info/` → identity / store id). Today: advertiser
     `7611255872546701319` (OL Beauty), store `7494362432882967723`. The Smashbox
     advertiser `7594559912720334865` has 0 GMV-Max campaigns.

2. **Pull the report** — `GET /gmv_max/report/get/`
   - `advertiser_id`
   - `store_ids = ["7494362432882967723"]`  ← **required**; GMV-Max is store-keyed
   - `dimensions = ["campaign_id", "stat_time_day"]`
   - `metrics = ["cost", "orders", "gross_revenue"]`  (also valid: `net_cost`,
     `roi`; `roi` is derivable as `gross_revenue / cost`)
   - `start_date` / `end_date` — **max 30-day window per call**
   - `page` / `page_size` (≤1000) — paginate on `data.page_info.total_page`
   - Sample row: `{"dimensions": {"campaign_id": "...", "stat_time_day":
     "2026-06-18 00:00:00"}, "metrics": {"cost": "82.04", "orders": "2",
     "gross_revenue": "50.40", "net_cost": "82.04", "roi": "0.61"}}`

Metrics that are NOT valid (probed and rejected): `sku_orders`, `order_cnt`,
`onsite_shopping_cnt`, `gmv`, `total_onsite_shopping_value`. The orders metric is
spelled **`orders`**.

## Mapping to our model

`GmvMaxDailyMetric` is uniquely keyed by `metric_date` and stores three additive
values; cost-per-order and ROI are derived at aggregation time.

| Our column      | API metric (summed over campaigns for the day) |
|-----------------|------------------------------------------------|
| `cost`          | `cost`                                         |
| `sku_orders`    | `orders`                                        |
| `gross_revenue` | `gross_revenue`                                |

## Architecture

Mirrors the **SAP inventory sync** (`app/services/inventory_sync.py`): a dedicated
service that fetches from an external API, reshapes into the DataFrame the existing
importer already consumes, and writes one `ImportBatch`. Triggered by a manual
button and a weekday cron.

### Components

**1. API client — extend `app/services/tiktok_marketing_api.py`**
Two thin, DB-free functions (pure HTTP + parse; unit-testable with mocked httpx):
- `list_gmv_max_campaigns(access_token, advertiser_id) -> list[dict]`
  — calls `/gmv_max/campaign/get/` with the promotion-type filter; returns
  campaigns (incl. their store ids, resolving via campaign info where needed).
- `get_gmv_max_report(access_token, advertiser_id, store_ids, start_date,
  end_date, page=1, page_size=1000) -> dict`
  — one page of `/gmv_max/report/get/`; returns `{"list": [...], "page_info":
  {...}}`. Caller paginates.

**2. Orchestration service — new `app/services/gmv_max_sync.py`**
`sync_gmv_max(db, *, lookback_days=35, today=None) -> ImportResult`:
1. Load the `TikTokMarketingCredential` (refresh token via existing path if
   needed). None → return a result flagged "no credential", no crash.
2. **Discover** advertiser(s) + GMV-Max campaigns + store ids. No GMV-Max
   campaigns anywhere → result flagged "no GMV-Max campaigns", 0 rows.
3. **Pull** the report per store over `[today - lookback_days, today]`, chunked
   into ≤30-day windows, paginated. A 30-day chunker helper splits the range.
4. **Aggregate** campaign×day rows → one row per `metric_date` (sum `cost`,
   `orders`, `gross_revenue`); build the importer DataFrame.
5. **Write** under one `ImportBatch(kind=TIKTOK_GMV_MAX)` via the importer's
   shared `import_dataframe(df, db, batch)` seam → idempotent upsert by
   `metric_date`. The router/caller commits.

**3. Importer seam — `app/importers/gmv_max_campaign.py`**
Factor the existing parse/write into `import_dataframe(df, db, batch) ->
ImportResult` so both the CSV `run(path, …)` and the API sync share one writer
(same approach inventory_snapshot uses). CSV upload path is unchanged externally.

**4. Trigger surface**
- **Button:** `POST /uploads/sync-gmv-max` in `app/routers/uploads.py`, next to the
  SAP "Sync inventory" button. Runs `sync_gmv_max` off the event loop (threadpool,
  like the SAP button), commits, 303-redirects with a flash ("Imported N days of
  GMV-Max spend" / the failure reason).
- **Cron:** in `app/services/scheduler.py`, the **existing weekday SAP job also
  calls `sync_gmv_max`** — one trigger, both feeds. No new schedule fields or
  toggle; reuses the SAP schedule/toggle and `SCHEDULER_ENABLED` gating. If the
  GMV-Max pull raises, it is logged and does not abort the SAP inventory sync
  (independent try/except within the job).

### Data flow

```
weekday cron (SAP job)  ─┐
manual button  ──────────┴─→ sync_gmv_max(db)
   → marketing creds → discover advertiser/store/campaigns
   → /gmv_max/report/get/ (≤30-day chunks, paginated)
   → aggregate campaign×day → by-day rows
   → ImportBatch(TIKTOK_GMV_MAX) + import_dataframe()  [upsert by metric_date]
   → GmvMaxDailyMetric  →  Ad Spend page / Attributed ROAS
```

### Look-back window

Default trailing **35 days** re-pulled each run. Rationale: TikTok revises recent
days and attribution backfills for a few days after; 35 covers a full month plus
revision lag while staying within two 30-day chunks. Overlapping days upsert in
place, so re-pulling is free of drift. A one-time wider backfill can be run by
calling the service with a larger `lookback_days` (or a small admin/CLI invocation)
to confirm historical parity and fill any gaps — not a scheduled concern.

### Error handling

- No marketing credential / not connected → result with `reason`, 0 rows, button
  shows it. (Not an exception.)
- No GMV-Max campaigns discovered → result with `reason`, 0 rows.
- API/HTTP error on any chunk → raise; the batch rolls back (no partial day-set
  committed); the message surfaces in the flash / scheduler log.
- Token expiry → refresh via the existing marketing-credential refresh, then retry
  once; still failing → treated as an API error above.
- In the cron, a GMV-Max failure is caught so it never aborts the SAP inventory
  sync that shares the job.

### Testing

No live API in tests. With mocked httpx responses / a fake client:
- **Aggregation:** campaign×day payload → correct by-day sums (`cost`,
  `sku_orders`←`orders`, `gross_revenue`); ROI/cost-per-order derive correctly
  downstream (already covered by Ad Spend tests).
- **Chunker:** a >30-day range splits into correct ≤30-day windows with no gaps or
  overlaps at the boundaries.
- **Idempotency:** running the sync twice over the same window yields identical row
  count and totals (upsert by `metric_date`).
- **Parity fixture:** a May-shaped mock payload sums to the known
  $7,824.02 / $15,769.65, guarding the mapping.
- **Discovery edge cases:** no credential and no-campaigns paths return flagged
  results (0 rows), not exceptions.
- **Importer seam:** `import_dataframe` writes/updates rows the same as the CSV
  `run` path (shared-writer regression).

## Out of scope (YAGNI)

- New schedule fields / a separate toggle (reusing the SAP schedule by decision).
- Per-campaign storage (`GmvMaxDailyMetric` is by-day; campaign dimension is summed
  away — matches today's table and the Ad Spend page).
- Multi-shop scoping beyond setting `shop_id` consistently with the CSV importer
  (Phase 2b, not this work).
- Backfilling `roi`/`net_cost` columns (derived, not stored).

## Success criteria

1. Clicking "Sync GMV-Max" on Uploads imports the trailing window and the Ad Spend
   page reflects it, ties to TikTok to the cent for settled days.
2. The weekday cron refreshes GMV-Max without manual action.
3. Re-running is idempotent (no row growth, no total drift).
4. Manual CSV upload still works as a fallback.
5. Full test suite green.
