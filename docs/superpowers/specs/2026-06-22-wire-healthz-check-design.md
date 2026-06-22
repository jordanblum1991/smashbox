# Wire the Fly `/healthz` Liveness Check — Design

**Date:** 2026-06-22
**Status:** Approved (design) — executed inline (config-only).

## Problem (and the measurement that reframed it)

The Fly `/healthz` liveness check was intentionally left **unwired**: the fly.toml
note claimed cold start was **~57s** vs Fly's 60s `grace_period` cap — too thin a
margin, so a slow boot could boot-loop the machine.

**Measured 2026-06-22, the ~57s figure is wrong.** On the real prod machine +
real Postgres:
- `import app.main` (heavy imports + `create_all` + `_ensure_columns`): **3.29s**
- `_bootstrap_shop_and_backfill` (idempotent): **0.03s**
- Fly microVM boot (logs): **1.07s**; image pull on a fresh deploy: **~8s**

→ Real **boot-to-ready ≈ 10–12s**, not 57s. (Module imports total ~2.5–3.3s, so
"lazy-import pandas" would save ~1s — there is nothing meaningful to optimize.)
The fly.toml even contradicted itself (`auto_start` comment says "~5s").

So the "cold-start reduction" half of the original idea is **moot**. The actual,
much smaller task: just **wire the check** with a sensible config.

## Change

In `fly.toml`, replace the "intentionally NOT wired" NOTE inside `[http_service]`
with an HTTP check on the existing `/healthz` endpoint:

```toml
  [[http_service.checks]]
    grace_period = "30s"
    interval = "15s"
    timeout = "5s"
    method = "GET"
    path = "/healthz"
```

- `grace_period = 30s` — ~3× the measured ~10s boot, safely under the 60s cap.
- `/healthz` is already DB-free + auth-exempt, so a locked/slow DB cannot fail the
  check and trigger a needless restart (the exact concern the endpoint was
  designed around).

## Why it's safe

- Real boot ~10s ≪ 30s grace ≪ 60s cap → no boot-loop risk.
- A single machine + rolling deploy means a **misconfigured check fails the deploy**
  (the new machine never goes healthy) rather than silently boot-looping a healthy
  prod — the deploy itself is the safety gate.
- Rollback = revert the `[[http_service.checks]]` block + redeploy.

## What it buys

Fly auto-restarts the machine if the app process **hangs** (event loop wedged but
machine up) — the one failure mode that neither the in-app sync alerts nor the
external dead-man's-switch recover *automatically*. Complementary layer:
- in-app alerts → a sync stream errors/stale (app healthy),
- dead-man's-switch (GitHub Actions) → machine fully down or scheduler thread dead,
- `/healthz` Fly check → app process hung but machine alive → auto-restart.

## Verification (the real work, prod-side)

1. `fly deploy` — the rolling update only completes if the new machine passes the
   `/healthz` check (boots ~10s ≪ 30s grace).
2. `fly checks list` / `fly status` → the `/healthz` check shows **passing**, the
   machine **healthy**, and **no restart loop** over a couple of minutes.
3. Confirm `fly releases` healthy + a normal prod page loads.

## Out of scope

- Any cold-start "reduction" / lazy-import work (measured unnecessary).
- Changing `auto_stop_machines` / `min_machines_running` (unrelated).

## Follow-up

Correct the stale "~57s" claim in the memory note
`project_uploads_nonblocking_and_deferred_healthcheck.md` → measured ~10s, check
now wired.

## Success criteria

`/healthz` check is wired in fly.toml, deploys cleanly (no boot-loop), shows
passing on prod, and the machine auto-restarts on a hung event loop.
