# Dead-Man's-Switch — External Scheduler Liveness Alert — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Problem

The sync-failure email alerts (shipped 2026-06-19) fire **from** the in-process
APScheduler job. So they cannot detect the two failure modes where the scheduler
itself isn't running:
1. **Machine/app fully down** (crash loop, Fly platform issue, OOM) — nothing
   runs, nothing emails.
2. **Web process alive but the scheduler thread died** — `/healthz` still says
   "ok", reports render, but no sync job (and therefore no alert check) ever
   fires again.

We need an **external** monitor — off the Fly machine — that notices either and
emails the team, independently of the app's own mailer (which is useless if the
app is down).

## Scope decisions (from brainstorming)

- **Monitor:** a **GitHub Actions** scheduled workflow (the repo already uses
  Actions; no new service/account). The alert email is sent **from the GitHub
  runner via SMTP**, so it works even when Fly is unreachable.
- **Signal:** a **scheduler heartbeat** (catches a dead scheduler thread, not just
  a down machine).
- **Recipients:** jordan@beautychoice.com, candice@beautychoice.com (same as the
  in-app alerts).
- **Cadence:** 15-min heartbeat · 1h stale threshold · hourly external check
  (revisit later if mis-tuned).
- **Keep it dead simple:** the external check is a stateless `curl → if bad,
  email`. No state machine in the safety net itself — a monitor with its own
  moving parts is one that can fail silently. (Consequence: a *prolonged* outage
  emails hourly until fixed; accepted for v1, see Out of Scope.)

## Architecture

Three small, independent units.

### 1. Scheduler heartbeat — `app/services/scheduler.py`

- A module global `_heartbeat: datetime | None` plus `record_heartbeat()` (sets it
  to `_utc_now_naive()`) and `heartbeat_status(*, now=None) -> dict` (the
  freshness verdict). Safe as a process global because the app is a **single
  always-on machine, single process** (the existing "exactly one instance"
  invariant the scheduler already relies on).
- `start_scheduler()` (only runs when `settings.scheduler_enabled`) **seeds**
  `_heartbeat = now` (so it's fresh immediately after a deploy) and registers a
  lightweight **15-minute** interval job (`HEARTBEAT_JOB_ID`) whose only work is
  `record_heartbeat()`. If the scheduler thread dies, the heartbeat stops
  advancing.
- `heartbeat_status(now)` returns:
  - `{"status": "disabled"}` when `not settings.scheduler_enabled` (dev/tests —
    never alarms).
  - `{"status": "ok", "heartbeat_age_s": N}` when fresh (`age < HEARTBEAT_STALE_S`
    = 3600).
  - `{"status": "stale", "heartbeat_age_s": N}` when stale, or
    `{"status": "stale", "heartbeat_age_s": null}` when enabled but no heartbeat
    recorded (defensive — shouldn't occur post-start).

### 2. Status endpoint — `GET /status/sync` (`app/main.py`)

- Auth-exempt (added to `SessionAuthMiddleware` exempt prefixes, like `/healthz`)
  and excluded from the request-time diagnostic middleware that runs DB queries
  (the `/static`,`/healthz` skip-list) — so it stays fast and DB-free.
- Returns `heartbeat_status()` as JSON with the HTTP status code:
  - `disabled` or `ok` → **200**
  - `stale` → **503**
- No DB, no auth — a plain liveness/freshness probe an external curl can read.

### 3. External monitor — `.github/workflows/deadman.yml`

- Triggers: `schedule` (cron `0 * * * *`, hourly) + `workflow_dispatch` (manual
  run for testing).
- One job, stateless:
  1. **Check** `https://smashbox.fly.dev/status/sync` with **retries** — up to 3
     attempts ~45s apart — treating both a non-200 HTTP status and an
     unreachable/timeout as a failure. Success on any attempt → pass, no email.
  2. **On confirmed failure** → send the email with an **inline Python step using
     stdlib `smtplib`** (the runner has Python; no third-party action ever
     receives our SMTP password — important for a safety net). It reads **GitHub
     repo secrets** (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`) from
     env and emails the hardcoded recipients (jordan@ + candice@beautychoice.com —
     not secret). Subject names the condition (unreachable vs `503 stale`); body
     includes the timestamp + a link to the Fly dashboard. Mirrors the app's own
     `app/services/mailer.py` (STARTTLS on 587).
- The email is produced on GitHub's infrastructure, so a fully-down Fly machine
  still triggers it.

## Data flow

```
scheduler thread alive → 15-min job → record_heartbeat() → _heartbeat = now
GitHub Actions (hourly) → GET /status/sync   (3 retries before deciding)
   200 ok        → pass, no email
   503 stale     → scheduler thread dead → email ops (GitHub SMTP)
   unreachable   → machine/app down       → email ops (GitHub SMTP)
```

## Error handling / false-alarm avoidance

- **Retries** ride out a transient network blip or an in-progress deploy. A deploy
  restarts the machine (~57s), after which `start_scheduler()` re-seeds the
  heartbeat to boot time → `/status/sync` is `200` again — so a deploy that
  overlaps the hourly check is absorbed by the retry window.
- **Dev/tests never alarm:** `scheduler_enabled` is False there → `disabled` →
  200.
- The endpoint is DB-free, so a DB hiccup can't make it 503 (we're probing the
  scheduler's liveness, not the DB's).

## Testing

In-repo (pytest):
- `record_heartbeat()` sets the timestamp; `heartbeat_status` returns `ok` when
  fresh, `stale` when the injected `now` is past the threshold, `disabled` when
  the scheduler is off (monkeypatch `settings.scheduler_enabled`).
- `GET /status/sync` returns 200 + `disabled` (default test env), 200 + `ok` after
  a `record_heartbeat()`, and 503 + `stale` when the heartbeat is forced old.

Not pytest-testable (documented manual verification):
- The GitHub workflow: trigger it via `workflow_dispatch` against prod (expect a
  pass while healthy); temporarily point the check at a always-503/unreachable URL
  (or lower the stale threshold) once to confirm the email path fires. Verify the
  email lands at both recipients.

## Out of scope (YAGNI)

- Edge-triggered dedup / "recovered" emails for the external check — kept
  **stateless** on purpose (reliability of the safety net > email tidiness). If a
  prolonged outage's hourly nag proves annoying, add a minimal once-only later.
- The cold-start / `/healthz`-wiring work (separate sub-project B).
- SMS, paging, broader uptime SLA, multi-region.

## Success criteria

1. `GET /status/sync` returns 200 when the scheduler heartbeat is fresh, 503 when
   stale, 200 `disabled` in non-prod.
2. The hourly GitHub Actions workflow passes silently while healthy and emails
   both recipients (from GitHub, via SMTP) when the endpoint is unreachable or
   503 — confirmed by a manual `workflow_dispatch` + a simulated failure.
3. The heartbeat re-seeds on deploy so routine deploys don't false-alarm.
4. Full pytest suite green; the safety net has no in-app state to corrupt.
