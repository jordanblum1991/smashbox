# Sync Failure Email Alerts — Design

**Date:** 2026-06-19
**Status:** Approved (design)

## Problem

Every financial/operational sync now runs unattended on the in-process scheduler
(TikTok orders/settlements/payouts/analytics daily; GMV-Max + SAP inventory on the
weekday job). A failure or stall only surfaces as a **passive nav badge**
(`sync_health` → `request.state.tiktok_sync_alert`). If nobody opens the app, stale
financials can go unnoticed for days. We want an **active email alert** when a
scheduled sync fails or goes stale, and a **recovery email** when it clears.

## Scope decisions (from brainstorming)

- **Channel:** email, via stdlib `smtplib` (no new dependency; fits the team's
  Google Workspace — Gmail SMTP + an app password).
- **Coverage:** all scheduled syncs — TikTok streams (via per-stream state),
  GMV-Max, and SAP inventory.
- **Recovery emails:** yes — one email when a previously-alerted condition clears.
- **Cadence:** once only — alert once per condition on a new failure, then silent
  until it recovers (edge-triggered; the condition re-arms after recovery).

## Architecture

A failure is an **alert condition** with a stable `key`. A tiny state machine
(persisted in a `SyncAlert` row per key) emails on the `ok → alerting` edge
(failure) and the `alerting → ok` edge (recovery). No time-window dedup.

### 1. Mailer — `app/services/mailer.py`

```python
def send_email(subject: str, body: str, *, to: list[str]) -> None
```
Stdlib `smtplib`: connect to `settings.smtp_host:smtp_port`, STARTTLS, login with
`smtp_user`/`smtp_password`, send a plain-text message from `settings.sync_alert_from`
to `to`. The single I/O seam — tests monkeypatch `smtplib.SMTP`. Raises on send
failure (callers catch + log; an email failure must never break a sync).

### 2. Evaluator + dispatcher — `app/services/sync_alerts.py`

```python
@dataclass(frozen=True)
class AlertCondition:
    key: str        # stable: "tiktok:settlements", "tiktok:stale", "gmv_max", "inventory"
    title: str      # "TikTok settlements sync failed"
    message: str    # the recorded error/detail (first line)

def evaluate_sync_alerts(db) -> list[AlertCondition]
def run_alert_check(db) -> None
```

**`evaluate_sync_alerts`** — the active problems right now, from the existing state:
- **TikTok streams:** for each `TikTokSyncState` with `last_status == "error"`, a
  condition `tiktok:<stream>` (message = `last_message`). Only when the shop is
  connected (`tiktok_api.get_credential(db).shop_cipher`), mirroring `sync_health`.
- **TikTok stale:** when connected + `tiktok_auto_sync_enabled` and the newest
  `last_run_at` across streams is older than `tiktok_sync.STALE_HOURS` (36h) → one
  `tiktok:stale` condition. (Reuses the existing constant.)
- **GMV-Max:** the most-recent `TIKTOK_GMV_MAX` `ImportBatch`; if its status is
  `FAILED` → `gmv_max` (message = `error_message`).
- **SAP inventory:** the most-recent SAP-sourced `INVENTORY_SNAPSHOT` batch
  (filename starts `"SAP"`); if `FAILED` → `inventory`.

**`run_alert_check`** — edge-triggered dispatch (never raises):
- No-op when `not settings.sync_alerts_enabled`.
- `active = {c.key: c for c in evaluate_sync_alerts(db)}`; `existing =
  {row.key: row for row in db.query(SyncAlert)}`.
- **New failure** (`key in active` and (no row OR row.state == "ok")): send the
  failure email; upsert `SyncAlert(state="alerting", message=…, last_transition_at=now)`.
- **Recovery** (`row.state == "alerting"` and `key not in active`): send the
  recovery email; set `state="ok"`, `last_transition_at=now`.
- Otherwise: nothing. Commit once.
- Each `send_email` is wrapped in try/except (log on failure, keep going) so one
  bad send doesn't strand the others or the sync job. On send failure the state is
  NOT advanced, so the next run retries the email.

Recipients = `settings.sync_alert_to_list` (parsed from a comma-separated setting),
falling back to `[settings.initial_admin_email]` when unset. Failure subject
`⚠ Smashbox sync alert: <title>`; recovery subject `✅ Smashbox sync recovered:
<title>`. Body: title, message, timestamp, and a link to the recon-health page
(`settings.public_base_url`).

### 3. Model — `app/models/sync_alert.py`

`SyncAlert`: `id`, `key` (String, unique), `state` (String, `"ok"|"alerting"`,
default `"ok"`), `message` (Text, nullable), `last_transition_at` (DateTime),
`created_at`/`updated_at`. New **Alembic migration** (+ `test_migrations` parity).

### 4. Config — `app/config.py`

Add: `smtp_host`, `smtp_port` (int, default 587), `smtp_user`, `smtp_password`,
`sync_alert_from`, `sync_alert_to` (comma-separated string). Derived:
```python
@property
def sync_alert_to_list(self) -> list[str]: ...   # parsed, or [initial_admin_email]
@property
def sync_alerts_enabled(self) -> bool:
    return bool(self.smtp_host and self.smtp_user and self.smtp_password
                and self.sync_alert_to_list)
```
So dev/tests/unconfigured prod are a clean no-op (mirrors `scheduler_enabled`).

### 5. Scheduler hook — `app/services/scheduler.py`

Call `run_alert_check(db)` at the END of both scheduled jobs
(`_run_tiktok_sync_job`, `_run_inventory_sync_job`), each in its own try/except so
an alerting failure never aborts a sync. The state machine is idempotent, so being
called from both jobs is harmless (no duplicate emails — state is already
`alerting`).

### 6. Manual test — `app/routers/uploads.py` + `uploads.html`

`POST /admin/sync-alerts/test` (admin-gated) sends a test email to
`sync_alert_to_list` so SMTP config can be verified without waiting for a real
failure; a small "Send test alert" button on the Uploads page, with a flashed
success/failure result. (Disabled-state shows a hint that SMTP isn't configured.)

## Data flow

```
scheduled job ends → run_alert_check(db)
  → evaluate_sync_alerts (TikTok state + GMV-Max/SAP batches)
  → diff vs SyncAlert rows → email on ok→alerting / alerting→ok
  → persist state
```

## Error handling

- SMTP/send error → caught + logged; state not advanced (retried next run); the
  sync job is unaffected.
- `sync_alerts_enabled` false → `run_alert_check` returns immediately.
- Not connected (no shop cipher) → TikTok conditions are skipped (GMV-Max/inventory
  still evaluated independently).

## Known limitation (explicitly out of scope)

Alerts fire **from** the scheduler. If the machine/scheduler itself never runs (a
full outage), no email fires — that requires **external** uptime monitoring (Fly
alerts or an external pinger). This is documented, not solved here.

## Testing

No real SMTP/network. With `smtplib.SMTP` monkeypatched and a temp SQLite DB:

- **Mailer:** `send_email` connects/STARTTLS/login/sends with the right
  from/to/subject/body; an SMTP exception propagates (caller's concern).
- **Evaluator:** an errored `TikTokSyncState` → a `tiktok:<stream>` condition; a
  stale watermark → `tiktok:stale`; a FAILED GMV-Max batch → `gmv_max`; a FAILED
  SAP batch → `inventory`; all-healthy → `[]`; not-connected skips TikTok
  conditions.
- **Edge-trigger (the core):** first failing run → one failure email + state
  `alerting`; a second still-failing run → **no** email (once-only); a recovery
  run → one recovery email + state `ok`; failing again after recovery → a fresh
  email (re-armed).
- **Disabled gate:** `sync_alerts_enabled` false → `run_alert_check` sends nothing.
- **Send-failure resilience:** a raising `send_email` is caught, the job/loop
  continues, and the state is NOT advanced (so it retries next run).
- **Scheduler hook:** both jobs call `run_alert_check` (mocked) and a failure in it
  doesn't abort the sync.
- **Manual test route:** `POST /admin/sync-alerts/test` calls the mailer and
  flashes the result; admin-gated.

## Out of scope (YAGNI)

- Per-user alert preferences, SMS/Slack (email only this round).
- Configurable thresholds beyond the existing 36h staleness.
- A dead-man's-switch for a fully-down scheduler (needs external monitoring).
- HTML email (plain text is enough for an internal ops alert).

## Success criteria

1. A failing scheduled sync sends exactly one failure email (per condition);
   recovery sends one email; a persisting failure does not re-spam.
2. Unconfigured SMTP (dev/tests/prod-without-secrets) is a clean no-op.
3. An email send failure never breaks a sync.
4. The manual test button verifies SMTP end-to-end.
5. New `sync_alerts` table via Alembic; full suite green.
