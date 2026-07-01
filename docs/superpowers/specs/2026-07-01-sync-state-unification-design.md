# Sync-state unification (Tier 2, item 6) — design

Status: **PLAN ONLY — not yet implemented.** Drafted 2026-07-01 alongside Tier 2
items 4 & 5 (which shipped). This is the large structural item deliberately
deferred from that pass.

## Problem

There are **8 auto-synced feeds** running on **3 different bookkeeping patterns**:

| Pattern | Feeds | State store | Watermark | Freshness alert (pre-Tier-1) |
| --- | --- | --- | --- | --- |
| Shop-stream | orders, settlements, payouts, analytics | `TikTokSyncState` | `synced_through` | error + coarse staleness |
| Marketing-stream | ad_spend | `TikTokSyncState` (stream=`ads`) | `synced_through` | error only |
| Batch-only | gmv_max, sap_sellable, sap_sample | `ImportBatch` | none (trailing window) | FAILED-batch only |

Because the feeds don't share one representation, every cross-cutting concern —
the status UI, the freshness display, and alerting — has to special-case each
pattern. That is the root cause of the blind spots Tier 1 patched **per-feed**:

- `sync_alerts._feed_staleness` hand-writes a per-feed list mixing
  `TikTokSyncState.synced_through` and `_latest_completed_batch_at(...)`.
- `data_freshness.py` tracks a *different*, hand-picked subset of `ImportBatch`
  kinds and omits analytics/ads/inventory.
- The status page (`/admin/tiktok`) renders only `TikTokSyncState`; the
  GMV-Max/SAP freshness lives on separate `/uploads` cards.

Each new feed or check means touching 3+ places and risks a new gap. Tier 1
closed today's gaps; unification prevents the *next* one by construction.

## Goal

One uniform per-feed sync record that **every** feed writes on every run, and
which **drives** the status UI, the dashboard freshness widget, and alerting —
so adding a feed or a check is a one-line registration, not a 3-file edit.

Non-goals: changing *what* each importer does, the trailing-window/self-heal
logic, or the actual fetch code. This is bookkeeping unification, not a rewrite
of the syncs.

## Target abstraction

A single `SyncFeed` registry + a single state row per feed.

```
SYNC_FEEDS = {
  "orders":       FeedSpec(title="TikTok orders",      domain="shop",      cadence="daily",   stale_h=36),
  "settlements":  FeedSpec(...),
  "payouts":      FeedSpec(...),
  "analytics":    FeedSpec(...),
  "ad_spend":     FeedSpec(title="TikTok ad-spend",    domain="marketing", cadence="daily",   stale_h=36),
  "gmv_max":      FeedSpec(title="GMV-Max",            domain="marketing", cadence="daily",   stale_h=36),
  "sap_sellable": FeedSpec(title="SAP sellable inv.",  domain="sap",       cadence="weekday", stale_h=80),
  "sap_sample":   FeedSpec(title="SAP sample inv.",    domain="sap",       cadence="weekday", stale_h=80),
}
```

`FeedSpec` carries: title, credential `domain` (shop | marketing | sap → drives
the connected-gate), a `cadence`/`stale_h` (drives staleness), and an `enabled`
resolver (reads `settings`/`Shop.*_enabled`).

**State row** (`SyncFeedState`, one per feed key), superset of today's
`TikTokSyncState`:

```
key            str  (PK)      # "orders", "gmv_max", …
last_run_at    dt              # every attempt
last_success_at dt | null     # advances only on ok/empty  ← the freshness signal
last_status    str            # ok | empty | pending | error
last_message   str | null
rows_last_run  int
updated_at     dt
```

`last_success_at` is the single freshness signal for staleness — it unifies
Shop's `synced_through`-as-proxy and batch `completed_at` into one column with
one meaning. (Keep `synced_through` on the Shop streams as the *incremental
watermark*; it's a separate concern from freshness and shouldn't be overloaded.)

## How each layer collapses

- **Writing:** a small `record_sync_run(db, key, status, rows, message)` helper
  that every sync entry point calls in its `finally`/success/except paths.
  Batch-only feeds (gmv_max, sap_*) call it in addition to writing their
  `ImportBatch` (batches stay — they're the row-level audit trail; the feed
  state is the summary).
- **Alerting:** `evaluate_sync_alerts` becomes: for each `SyncFeed`, if
  `enabled(db)` and `connected(domain)` → check `last_status == "error"` (with
  the `_actionable_marketing` decorator already added in item 5) and
  `now - last_success_at > stale_h`. The hand-written `_feed_staleness` list and
  the `_latest_completed_batch_at`/`_latest_batch_failed` batch-scanning helpers
  disappear.
- **Status UI:** `/admin/tiktok`, the `/uploads` cards, and the dashboard
  freshness widget all read the same `SyncFeedState` rows → one consistent view,
  every feed shown.
- **`data_freshness.py`:** folds into the same registry (drop the separate
  hand-picked `DATA_KINDS`).

## Migration / phasing (incremental — NOT big-bang)

The risk is that this touches every sync path. Do it in independently-shippable
phases, each green + deployed before the next:

1. **Introduce the model + writer, dual-write.** Add `SyncFeedState` (Alembic
   revision) + `record_sync_run`. Call it from every sync alongside the existing
   `TikTokSyncState`/`ImportBatch` writes. Nothing reads it yet. Backfill
   `last_success_at` from existing state/batches in the migration. *Ships dark.*
2. **Point alerting at the registry.** Rewrite `evaluate_sync_alerts` to read
   `SyncFeedState`. Delete `_feed_staleness` internals. Keep behavior identical
   (same keys, thresholds) — the Tier 1 tests are the safety net; they should
   pass unchanged.
3. **Point the status UI + dashboard freshness at the registry.** Remove the
   per-page special-casing.
4. **Retire the redundant reads of `TikTokSyncState`** for freshness (keep it
   only as the incremental watermark) and remove `data_freshness.DATA_KINDS`.

Each phase is one branch, TDD, deploy. Phase 1 is safe by construction
(write-only). Phases 2–3 are behavior-preserving swaps guarded by existing tests.

## Risks & mitigations

- **Colliding with active parallel work** — these files (`sync_alerts`,
  `tiktok_sync`, status templates) are hot. Mitigation: small phases, commit +
  merge fast, rebase often. (See `feedback_commit_increments_immediately`.)
- **Double source of truth during dual-write (phase 1)** — acceptable because
  nothing reads the new table until phase 2; the backfill makes it correct at
  cutover.
- **Behavior drift in alerting** — phase 2 must keep the exact alert keys +
  thresholds from Tier 1 so `test_sync_alerts.py` passes without edits (any edit
  needed = a behavior change to scrutinize).
- **Postgres migration** — new table needs an Alembic revision + the
  models↔migration parity guard (`test_migrations.py`); prod is Postgres.

## Testing

- Phase 1: `record_sync_run` upserts one row per key; each sync writes it;
  migration backfill populates `last_success_at`.
- Phase 2: reuse the entire Tier 1 `test_sync_alerts.py` suite unchanged as the
  behavior-preservation contract; add coverage that a feed with only a
  `SyncFeedState` row (no `ImportBatch`) still alerts.
- Phase 3: status page + dashboard render every feed from the registry.

## Estimate

~4 small branches. Phase 1 is the only new-schema step. Total is moderate but
low-risk *if* phased; high-risk only if attempted big-bang (why it was split out
of the Tier 2 hardening pass).
