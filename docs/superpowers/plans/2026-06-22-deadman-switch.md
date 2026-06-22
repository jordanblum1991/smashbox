# Dead-Man's-Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An external (GitHub Actions) monitor that emails the team when the Smashbox machine is down OR its in-process scheduler thread has died — failures the in-app alerts can't catch because they fire from that thread.

**Architecture:** A 15-minute scheduler heartbeat (in-process module global) + an auth-exempt `GET /status/sync` endpoint reporting heartbeat freshness (200 ok / 503 stale / 200 disabled) + an hourly GitHub Actions workflow that curls it (with retries) and, on a confirmed failure, emails ops via stdlib `smtplib` from the runner (independent of the app).

**Tech Stack:** FastAPI/Starlette, APScheduler, pytest; GitHub Actions + stdlib smtplib. Spec: `docs/superpowers/specs/2026-06-22-deadman-switch-design.md`.

**Branch:** `feature/deadman-switch` (created; spec committed).

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25` (NOT PowerShell/venv).
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` (Write tool), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Auth is disabled in tests.

---

## File Structure

- **Modify** `app/services/scheduler.py` — heartbeat globals + `record_heartbeat()` + `heartbeat_status()` + register the heartbeat job in `start_scheduler()` (Task 1).
- **Modify** `app/main.py` (endpoint + diagnostic skip-list) and `app/auth.py` (auth exempt prefixes) — the `GET /status/sync` endpoint (Task 2).
- **Create** `.github/workflows/deadman.yml` — the external monitor (Task 3).
- **Tests:** `tests/test_deadman.py` (heartbeat + endpoint + workflow-content guard).

---

## Task 1: Scheduler heartbeat

**Files:**
- Modify: `app/services/scheduler.py`
- Test: `tests/test_deadman.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deadman.py
"""Dead-man's-switch: scheduler heartbeat freshness + the /status/sync endpoint."""
from datetime import timedelta

import app.services.scheduler as sched
from app.config import settings


def test_heartbeat_disabled_when_scheduler_off(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", False, raising=False)
    assert sched.heartbeat_status()["status"] == "disabled"


def test_heartbeat_ok_after_record(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    sched.record_heartbeat()
    s = sched.heartbeat_status()
    assert s["status"] == "ok"
    assert s["heartbeat_age_s"] < 60


def test_heartbeat_stale_past_threshold(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    sched.record_heartbeat()
    later = sched._heartbeat + timedelta(seconds=sched.HEARTBEAT_STALE_S + 60)
    s = sched.heartbeat_status(now=later)
    assert s["status"] == "stale"
    assert s["heartbeat_age_s"] >= sched.HEARTBEAT_STALE_S


def test_heartbeat_none_is_stale_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    monkeypatch.setattr(sched, "_heartbeat", None, raising=False)
    s = sched.heartbeat_status()
    assert s["status"] == "stale"
    assert s["heartbeat_age_s"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_deadman.py -v 2>&1 | tail -20`
Expected: FAIL — `module 'app.services.scheduler' has no attribute 'heartbeat_status'` / `record_heartbeat` / `HEARTBEAT_STALE_S`.

- [ ] **Step 3: Add the heartbeat to `app/services/scheduler.py`**

Add the IntervalTrigger import near the existing `from apscheduler.triggers.cron import CronTrigger`:
```python
from apscheduler.triggers.interval import IntervalTrigger
```
Add `_utc_now_naive` import near the other `from app...` imports:
```python
from app.models.import_batch import _utc_now_naive
```
Add the constants + globals near the existing `INVENTORY_JOB_ID`/`TIKTOK_JOB_ID`:
```python
HEARTBEAT_JOB_ID = "scheduler_heartbeat"
HEARTBEAT_INTERVAL_MIN = 15   # heartbeat cadence
HEARTBEAT_STALE_S = 3600      # /status/sync goes 503 past this age

_heartbeat = None  # type: ignore[var-annotated]  # last proof-of-life, UTC-naive
```
Add the two functions (anywhere at module level, e.g. after the globals):
```python
def record_heartbeat() -> None:
    """Proof the scheduler thread is alive — called by the 15-min heartbeat job
    and seeded at start_scheduler()."""
    global _heartbeat
    _heartbeat = _utc_now_naive()


def heartbeat_status(*, now=None) -> dict:
    """Freshness verdict for the external dead-man's-switch.
      disabled — scheduler isn't running (dev/tests); never alarms.
      ok       — heartbeat age < HEARTBEAT_STALE_S.
      stale    — heartbeat too old, or absent while the scheduler is enabled.
    """
    if not settings.scheduler_enabled:
        return {"status": "disabled"}
    if _heartbeat is None:
        return {"status": "stale", "heartbeat_age_s": None}
    age = int(((now or _utc_now_naive()) - _heartbeat).total_seconds())
    return {"status": "ok" if age < HEARTBEAT_STALE_S else "stale", "heartbeat_age_s": age}
```
In `start_scheduler()`, after `_scheduler.start()` (and before/after the `apply_*` calls), seed the heartbeat and register the job:
```python
    record_heartbeat()
    _scheduler.add_job(
        record_heartbeat,
        trigger=IntervalTrigger(minutes=HEARTBEAT_INTERVAL_MIN),
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_deadman.py -v 2>&1 | tail -20`
Expected: 4 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
deadman-switch: scheduler heartbeat

A 15-min interval job updates an in-process heartbeat (seeded at
start_scheduler so deploys don't false-alarm); heartbeat_status() reports
ok/stale/disabled for the external monitor. A dead scheduler thread stops the
heartbeat advancing.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/services/scheduler.py tests/test_deadman.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: `GET /status/sync` endpoint (auth-exempt, DB-free)

**Files:**
- Modify: `app/main.py` (endpoint + diagnostic skip-list)
- Modify: `app/auth.py` (both `EXEMPT_PREFIXES` tuples)
- Test: `tests/test_deadman.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_deadman.py`:
```python
def test_status_sync_disabled_returns_200():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/status/sync")
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"


def test_status_sync_ok_returns_200(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    sched.record_heartbeat()
    r = TestClient(app).get("/status/sync")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_status_sync_stale_returns_503(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "scheduler_enabled", True, raising=False)
    monkeypatch.setattr(sched, "_heartbeat",
                        sched._utc_now_naive() - timedelta(seconds=sched.HEARTBEAT_STALE_S + 120),
                        raising=False)
    r = TestClient(app).get("/status/sync")
    assert r.status_code == 503
    assert r.json()["status"] == "stale"


def test_status_sync_reachable_without_redirect():
    # auth-exempt: must not 3xx-redirect to /login.
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/status/sync", follow_redirects=False)
    assert r.status_code in (200, 503)
```
(`timedelta` and `sched`/`settings` are already imported at the top of the file from Task 1.)

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_deadman.py -k status_sync -v 2>&1 | tail -20`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the endpoint in `app/main.py`**

Ensure `JSONResponse` is imported (the file already imports `PlainTextResponse` from `fastapi.responses`):
```python
from fastapi.responses import JSONResponse, PlainTextResponse
```
Add the route next to the existing `healthz` handler:
```python
@app.get("/status/sync", include_in_schema=False)
async def status_sync() -> JSONResponse:
    """External dead-man's-switch probe: reports whether the in-process scheduler
    heartbeat is fresh. DB-free + auth-exempt (see EXEMPT_PREFIXES). 503 when stale
    so a polling monitor can alert on a dead scheduler / down machine."""
    from app.services.scheduler import heartbeat_status

    s = heartbeat_status()
    return JSONResponse(s, status_code=503 if s["status"] == "stale" else 200)
```
Update the diagnostic-middleware skip-list (the line `if not request.url.path.startswith(("/static", "/healthz")):`) to also skip `/status`:
```python
    if not request.url.path.startswith(("/static", "/healthz", "/status")):
```

- [ ] **Step 4: Make the endpoint auth-exempt in `app/auth.py`**

There are TWO `EXEMPT_PREFIXES` tuples (one per auth middleware). Add `"/status"` to BOTH so the probe is reachable regardless of which middleware is active:
- The line `EXEMPT_PREFIXES = ("/static/", "/login", "/logout", "/healthz", "/auth/google", "/auth/tiktok")` → add `"/status"`.
- The line `EXEMPT_PREFIXES = ("/static/", "/healthz")` → add `"/status"`.

- [ ] **Step 5: Run the tests + an auth regression**

Run: `py -m pytest tests/test_deadman.py -v 2>&1 | tail -20`
Expected: 8 passed (4 from Task 1 + 4 new).
Run: `py -m pytest -k "auth or healthz or middleware" -q 2>&1 | tail -10`
Expected: pass (no auth regression).

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
deadman-switch: GET /status/sync probe endpoint

Auth-exempt, DB-free endpoint returning the scheduler heartbeat freshness
(200 ok / 503 stale / 200 disabled). Added /status to both auth exempt-prefix
lists and the diagnostic-middleware skip-list.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/main.py app/auth.py tests/test_deadman.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: GitHub Actions monitor workflow

**Files:**
- Create: `.github/workflows/deadman.yml`
- Test: `tests/test_deadman.py` (content guard)

- [ ] **Step 1: Add the failing content-guard test**

Append to `tests/test_deadman.py`:
```python
def test_deadman_workflow_has_key_content():
    """Guard the safety net's own content so a malformed/edited workflow that
    would silently never alert is caught in CI."""
    from pathlib import Path
    wf = Path(".github/workflows/deadman.yml").read_text(encoding="utf-8")
    assert "cron:" in wf and "0 * * * *" in wf          # hourly schedule
    assert "workflow_dispatch" in wf                     # manual trigger
    assert "/status/sync" in wf                          # probes the endpoint
    assert "smtplib" in wf                               # stdlib mailer (no 3rd-party action)
    assert "secrets.SMTP_PASSWORD" in wf                 # uses repo secrets
    assert "jordan@beautychoice.com" in wf and "candice@beautychoice.com" in wf
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_deadman.py::test_deadman_workflow_has_key_content -v 2>&1 | tail -12`
Expected: FAIL — file not found.

- [ ] **Step 3: Create `.github/workflows/deadman.yml`**

```yaml
name: deadman-switch

# External liveness monitor for the Smashbox Fly app. Runs on GitHub's infra so
# it can alert even when Fly is fully down. Probes /status/sync (the scheduler
# heartbeat); on a confirmed unreachable/503 it emails ops via stdlib smtplib.
on:
  schedule:
    - cron: "0 * * * *"   # hourly
  workflow_dispatch: {}

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Probe /status/sync (3 retries)
        id: probe
        run: |
          URL="https://smashbox.fly.dev/status/sync"
          ok=false
          code="000"
          for i in 1 2 3; do
            code=$(curl -s -o /tmp/body -w "%{http_code}" --max-time 20 "$URL" || echo "000")
            echo "attempt $i: HTTP $code"
            if [ "$code" = "200" ]; then ok=true; break; fi
            sleep 45
          done
          {
            echo "healthy=$ok"
            echo "last_code=$code"
          } >> "$GITHUB_OUTPUT"

      - name: Email ops on failure (stdlib smtplib)
        if: steps.probe.outputs.healthy != 'true'
        env:
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          LAST_CODE: ${{ steps.probe.outputs.last_code }}
        run: |
          python - <<'PY'
          import os, smtplib
          from email.message import EmailMessage

          code = os.environ.get("LAST_CODE", "000")
          cond = "unreachable (machine/app down)" if code in ("000", "") else f"HTTP {code} (scheduler stale)"
          msg = EmailMessage()
          msg["Subject"] = f"⚠ Smashbox DEAD-MAN'S-SWITCH: scheduler {cond}"
          msg["From"] = os.environ["SMTP_USER"]
          msg["To"] = "jordan@beautychoice.com, candice@beautychoice.com"
          msg.set_content(
              "The external monitor could not confirm the Smashbox scheduler is alive.\n\n"
              f"Probe result: {cond}\n"
              "Endpoint: https://smashbox.fly.dev/status/sync\n"
              "Fly dashboard: https://fly.io/apps/smashbox\n\n"
              "This means the machine is down OR the in-process scheduler thread died "
              "(the in-app sync alerts cannot fire in that case)."
          )
          with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
              s.starttls()
              s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
              s.send_message(msg)
          print("dead-man's-switch alert email sent")
          PY

      - name: Mark the run failed when unhealthy
        if: steps.probe.outputs.healthy != 'true'
        run: |
          echo "::error::/status/sync unhealthy (last HTTP ${{ steps.probe.outputs.last_code }})"
          exit 1
```

- [ ] **Step 4: Run the content guard + full deadman tests**

Run: `py -m pytest tests/test_deadman.py -v 2>&1 | tail -20`
Expected: 9 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
deadman-switch: hourly GitHub Actions monitor

Cron workflow curls /status/sync (3 retries to absorb deploys/blips) and, on a
confirmed unreachable/503, emails ops via stdlib smtplib from the runner using
repo secrets — independent of the (possibly-down) Fly app. A content-guard test
keeps the safety net from being silently broken.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add .github/workflows/deadman.yml tests/test_deadman.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: Full suite + deploy + handoff

**Files:** none (verification + ship + handoff)

- [ ] **Step 1: Full suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 852 + the new deadman tests; 11 skipped).

- [ ] **Step 2: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/deadman-switch
git checkout main && git pull --ff-only
git merge --no-ff feature/deadman-switch -m "Merge feature/deadman-switch"
git push origin main
git branch -d feature/deadman-switch && git push origin --delete feature/deadman-switch
fly deploy
```
No schema change → the release `alembic upgrade head` is a no-op.

- [ ] **Step 3: Verify the endpoint on prod**

After release, `curl -s -o /dev/null -w "%{http_code}\n" https://smashbox.fly.dev/status/sync` → expect **200** (scheduler enabled + freshly seeded heartbeat). `curl -s https://smashbox.fly.dev/status/sync` → `{"status":"ok","heartbeat_age_s":N}`. Confirm via `fly releases` healthy.

- [ ] **Step 4: Hand off GitHub secrets + manual verification (USER)**

Tell the user (do NOT do these yourself):
1. Add the SMTP values as **GitHub repo secrets** (Settings → Secrets and variables → Actions): `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=smashbox2026@gmail.com`, `SMTP_PASSWORD=<the Gmail app password>`. (These are separate from the Fly secrets — GitHub Actions can't read Fly secrets.)
2. **Verify the pass path:** GitHub → Actions → "deadman-switch" → "Run workflow" (workflow_dispatch). Expect a green run with no email (endpoint is healthy).
3. **Verify the alert path once:** temporarily edit the workflow's `URL` to a guaranteed-bad value (e.g. `https://smashbox.fly.dev/status/does-not-exist`) on a throwaway branch and Run workflow → expect the failure email at both recipients, then revert. (Or lower `HEARTBEAT_STALE_S` briefly — but the bad-URL route is simpler and touches no prod code.)

---

## Self-Review

**Spec coverage:**
- 15-min heartbeat seeded at start, in-process global → Task 1. ✓
- `heartbeat_status` ok/stale/disabled → Task 1. ✓
- `GET /status/sync` auth-exempt, DB-free, 200/503/disabled → Task 2 (endpoint + both EXEMPT_PREFIXES + diagnostic skip). ✓
- Hourly GitHub Actions, retries, stdlib-smtplib email from runner to both recipients, repo secrets → Task 3. ✓
- Deploy re-seeds heartbeat (no false alarm) → Task 1 (`start_scheduler` seeds) + Task 4 Step 3 (prod 200 right after deploy). ✓
- Stateless / no dedup (reliability) → Task 3 (no state machine). ✓
- Testing: heartbeat freshness, endpoint codes, workflow content guard → Tasks 1–3; manual workflow verification → Task 4. ✓
- Out-of-scope (cold-start B, SMS, dedup) honored. ✓

**Placeholder scan:** No TBD/TODO; every code step is complete. The "bad URL" verification is concrete.

**Type consistency:** `record_heartbeat() -> None`, `heartbeat_status(*, now=None) -> dict` (keys `status`, `heartbeat_age_s`), module globals `_heartbeat`/`HEARTBEAT_STALE_S`/`HEARTBEAT_INTERVAL_MIN`/`HEARTBEAT_JOB_ID` are referenced identically across Tasks 1–2 and the tests. The endpoint maps `status=="stale"`→503 consistently. The workflow secret names (`SMTP_HOST/PORT/USER/PASSWORD`) match the handoff step. ✓
