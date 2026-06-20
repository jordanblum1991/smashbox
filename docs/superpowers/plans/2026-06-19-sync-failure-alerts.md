# Sync Failure Email Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Email an alert when any scheduled sync (TikTok streams, GMV-Max, SAP inventory) fails or goes stale, and a recovery email when it clears — once-only, edge-triggered, no-op until SMTP secrets are set.

**Architecture:** A stdlib-`smtplib` mailer seam, an evaluator that turns current sync state into `AlertCondition`s, and an edge-triggered dispatcher backed by a `SyncAlert(key, state)` row (emails on `ok→alerting` and `alerting→ok`). Wired into the end of both scheduler jobs, plus a manual "send test" admin button.

**Tech Stack:** FastAPI/Starlette, SQLAlchemy 2.x, Alembic, stdlib smtplib/email, pytest. Spec: `docs/superpowers/specs/2026-06-19-sync-failure-alerts-design.md`.

**Branch:** `feature/sync-failure-alerts` (created; spec committed).

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -30` (NOT PowerShell/venv).
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` (Write tool), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Money is Decimal (n/a here). Auth disabled in tests (admin routes work in TestClient — see `tests/test_ad_budget.py`).

---

## File Structure

- **Modify** `app/config.py` — SMTP settings + `sync_alert_to_list` + `sync_alerts_enabled` (Task 1).
- **Create** `app/services/mailer.py` — `send_email` (Task 2).
- **Create** `app/models/sync_alert.py` + **modify** `app/models/__init__.py` + **create** `alembic/versions/b1c2d3e4f5a6_sync_alerts.py` (Task 3).
- **Create** `app/services/sync_alerts.py` — evaluator + dispatcher (Task 4).
- **Modify** `app/services/scheduler.py` + `app/routers/uploads.py` + `app/templates/uploads.html` (Task 5).
- **Tests:** `tests/test_mailer.py`, `tests/test_sync_alerts.py`, `tests/test_sync_alerts_button.py`.

---

## Task 1: Config — SMTP settings + enabled gate

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_sync_alert_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_alert_config.py
"""SMTP/alert settings: recipient parsing + the enabled gate."""
from app.config import Settings


def test_sync_alert_to_list_parses_comma_and_falls_back():
    s = Settings(sync_alert_to="a@x.com, b@x.com")
    assert s.sync_alert_to_list == ["a@x.com", "b@x.com"]
    s2 = Settings(sync_alert_to="", initial_admin_email="admin@x.com")
    assert s2.sync_alert_to_list == ["admin@x.com"]
    s3 = Settings(sync_alert_to="", initial_admin_email="")
    assert s3.sync_alert_to_list == []


def test_sync_alerts_enabled_requires_full_smtp_config():
    off = Settings()
    assert off.sync_alerts_enabled is False
    on = Settings(smtp_host="smtp.gmail.com", smtp_user="u@x.com",
                  smtp_password="pw", sync_alert_to="a@x.com")
    assert on.sync_alerts_enabled is True
    # missing password → still off
    partial = Settings(smtp_host="smtp.gmail.com", smtp_user="u@x.com",
                       sync_alert_to="a@x.com")
    assert partial.sync_alerts_enabled is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sync_alert_config.py -v 2>&1 | tail -15`
Expected: FAIL — `Settings` has no `sync_alert_to_list` / unexpected kwarg `smtp_host`.

- [ ] **Step 3: Add the settings**

In `app/config.py`, inside the `Settings` class (near the other plain fields, e.g. after `public_base_url`), add:
```python
    # --- Sync-failure email alerts (stdlib smtplib). All blank by default, so
    #     alerting is a no-op until these Fly secrets are set. ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sync_alert_from: str = ""              # falls back to smtp_user in the mailer
    sync_alert_to: str = ""               # comma-separated recipients
```
And add these properties (next to the other `@property` defs in the class):
```python
    @property
    def sync_alert_to_list(self) -> list[str]:
        """Recipients, parsed from the comma-separated setting; falls back to the
        initial admin email when unset."""
        raw = [a.strip() for a in (self.sync_alert_to or "").split(",") if a.strip()]
        if raw:
            return raw
        return [self.initial_admin_email] if self.initial_admin_email else []

    @property
    def sync_alerts_enabled(self) -> bool:
        """Alerting fires only when SMTP is fully configured AND there's a
        recipient — so dev/tests/unconfigured prod are a clean no-op."""
        return bool(self.smtp_host and self.smtp_user and self.smtp_password
                    and self.sync_alert_to_list)
```

- [ ] **Step 4: Run the test**

Run: `py -m pytest tests/test_sync_alert_config.py -v 2>&1 | tail -15`
Expected: 2 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sync alerts: SMTP/alert config + enabled gate

Add smtp_* + sync_alert_from/to settings, sync_alert_to_list (comma parse,
falls back to initial_admin_email), and sync_alerts_enabled (true only when
SMTP is fully configured) so alerting is a no-op until secrets are set.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/config.py tests/test_sync_alert_config.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: Mailer (stdlib smtplib)

**Files:**
- Create: `app/services/mailer.py`
- Test: `tests/test_mailer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mailer.py
"""The stdlib-smtplib mailer seam. No real network — smtplib.SMTP is mocked."""
import smtplib

import app.services.mailer as mailer
from app.config import settings


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"], msg["From"]))


def test_send_email_uses_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_port", 587, raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u@x.com", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(settings, "sync_alert_from", "", raising=False)

    mailer.send_email("Hi", "body here", to=["a@x.com", "b@x.com"])

    smtp = _FakeSMTP.instances[-1]
    assert smtp.host == "smtp.example.com" and smtp.port == 587
    assert "starttls" in smtp.calls
    assert ("login", "u@x.com") in smtp.calls
    sent = [c for c in smtp.calls if c[0] == "send"][0]
    assert sent[1] == "a@x.com, b@x.com"        # To
    assert sent[2] == "Hi"                         # Subject
    assert sent[3] == "u@x.com"                     # From falls back to smtp_user
```

(If `monkeypatch.setattr(settings, ...)` raises because the pydantic model is frozen, set `model_config` allows mutation by default; if not, the implementer may instead construct a Settings and monkeypatch `mailer.settings`. Use whichever works; do NOT change production code to satisfy the test.)

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_mailer.py -v 2>&1 | tail -15`
Expected: FAIL — no module `app.services.mailer`.

- [ ] **Step 3: Create the mailer**

```python
# app/services/mailer.py
"""Outbound email via stdlib smtplib — the single send seam for sync-failure
alerts. No third-party dependency. SMTP config comes from app.config; tests
monkeypatch smtplib.SMTP. Raises on send failure (the caller decides what to do).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def send_email(subject: str, body: str, *, to: list[str]) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sync_alert_from or settings.smtp_user
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
```

- [ ] **Step 4: Run the test**

Run: `py -m pytest tests/test_mailer.py -v 2>&1 | tail -15`
Expected: 1 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sync alerts: stdlib smtplib mailer seam

send_email(subject, body, *, to) builds an EmailMessage and sends via STARTTLS
using the SMTP config. The single isolated send path; raises on failure.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/services/mailer.py tests/test_mailer.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: SyncAlert model + migration

**Files:**
- Create: `app/models/sync_alert.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/b1c2d3e4f5a6_sync_alerts.py`
- Test: `tests/test_sync_alert_model.py` + the existing `tests/test_migrations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_alert_model.py
"""SyncAlert persists per-condition alert state."""
import pytest

from app.db import Base, SessionLocal, engine
from app.models.sync_alert import SyncAlert


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_sync_alert_roundtrip():
    with SessionLocal() as db:
        db.add(SyncAlert(key="tiktok:settlements", state="alerting", message="boom"))
        db.commit()
        row = db.query(SyncAlert).filter_by(key="tiktok:settlements").one()
        assert row.state == "alerting"
        assert row.message == "boom"
        assert row.last_transition_at is not None


def test_sync_alert_in_metadata():
    assert "sync_alerts" in Base.metadata.tables
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sync_alert_model.py -v 2>&1 | tail -15`
Expected: FAIL — no module `app.models.sync_alert`.

- [ ] **Step 3: Create the model + register it**

`app/models/sync_alert.py`:
```python
"""Per-condition state for the sync-failure email alerter.

One row per stable alert `key` (e.g. "tiktok:settlements", "gmv_max"). The
edge-triggered alerter (app/services/sync_alerts.py) emails on the ok→alerting
and alerting→ok transitions; this row remembers the current state so a persisting
failure doesn't re-spam and a recovery emails exactly once.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class SyncAlert(Base):
    __tablename__ = "sync_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)  # ok|alerting
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_transition_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive, nullable=False)
```

In `app/models/__init__.py`, add an import next to the other model imports:
```python
from app.models.sync_alert import SyncAlert
```
and add `"SyncAlert",` to the `__all__` list.

- [ ] **Step 4: Create the Alembic migration**

`alembic/versions/b1c2d3e4f5a6_sync_alerts.py`:
```python
"""sync_alerts table

Per-condition state for the sync-failure email alerter.

Revision ID: b1c2d3e4f5a6
Revises: a1b8c2d3e4f5
Create Date: 2026-06-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a1b8c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("last_transition_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sync_alerts_key"), "sync_alerts", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_sync_alerts_key"), table_name="sync_alerts")
    op.drop_table("sync_alerts")
```

- [ ] **Step 5: Run model + migration-parity tests**

Run: `py -m pytest tests/test_sync_alert_model.py tests/test_migrations.py -v 2>&1 | tail -20`
Expected: all pass — including `tests/test_migrations.py` (the model↔migration parity guard), confirming the migration matches the model's table + columns. If parity fails, align the migration columns to the model exactly.

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sync alerts: SyncAlert model + migration

One row per alert key tracking state (ok|alerting) + last_transition_at, so the
edge-triggered alerter emails once per failure and once on recovery. New
Alembic revision b1c2d3e4f5a6 (head a1b8c2d3e4f5).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/models/sync_alert.py app/models/__init__.py alembic/versions/b1c2d3e4f5a6_sync_alerts.py tests/test_sync_alert_model.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: sync_alerts service (evaluator + edge-triggered dispatcher)

**Files:**
- Create: `app/services/sync_alerts.py`
- Test: `tests/test_sync_alerts.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync_alerts.py
"""evaluate_sync_alerts (conditions from sync state) + run_alert_check (the
edge-triggered email state machine). No network — mailer + evaluator stubbed."""
from datetime import datetime, timedelta

import pytest

import app.services.sync_alerts as sa
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.sync_alert import SyncAlert
from app.services.sync_alerts import AlertCondition, run_alert_check


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def enabled(monkeypatch):
    # Make settings.sync_alerts_enabled True + a recipient, without real SMTP.
    monkeypatch.setattr(settings, "smtp_host", "h", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(settings, "sync_alert_to", "a@x.com", raising=False)


def _failed_batch(db, kind, fname):
    db.add(ImportBatch(kind=kind, status=ImportBatchStatus.FAILED,
                       original_filename=fname, stored_path="", error_message="boom"))
    db.commit()


def test_evaluate_flags_failed_gmv_and_inventory_batches():
    with SessionLocal() as db:
        _failed_batch(db, ImportFileKind.TIKTOK_GMV_MAX, "TikTok GMV-Max API sync")
        _failed_batch(db, ImportFileKind.INVENTORY_SNAPSHOT, "SAP SB+SBS sync")
        conds = sa.evaluate_sync_alerts(db)
    keys = {c.key for c in conds}
    assert "gmv_max" in keys and "inventory" in keys


def test_evaluate_healthy_returns_none():
    with SessionLocal() as db:
        db.add(ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX,
                           status=ImportBatchStatus.COMPLETED,
                           original_filename="TikTok GMV-Max API sync", stored_path=""))
        db.commit()
        assert sa.evaluate_sync_alerts(db) == []


def test_edge_trigger_failure_then_recovery(monkeypatch, enabled):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    conds = [AlertCondition("gmv_max", "GMV-Max sync failed", "boom")]
    monkeypatch.setattr(sa, "evaluate_sync_alerts", lambda db: list(conds))

    with SessionLocal() as db:
        run_alert_check(db)                      # new failure → 1 email
        assert len(sent) == 1 and "alert" in sent[0].lower()
        run_alert_check(db)                      # still failing → no email
        assert len(sent) == 1
        conds.clear()
        run_alert_check(db)                      # recovered → 1 email
        assert len(sent) == 2 and "recover" in sent[1].lower()
        row = db.query(SyncAlert).filter_by(key="gmv_max").one()
        assert row.state == "ok"


def test_re_arms_after_recovery(monkeypatch, enabled):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    conds = [AlertCondition("gmv_max", "GMV-Max sync failed", "boom")]
    monkeypatch.setattr(sa, "evaluate_sync_alerts", lambda db: list(conds))
    with SessionLocal() as db:
        run_alert_check(db)        # fail → email 1
        conds.clear(); run_alert_check(db)   # recover → email 2
        conds.append(AlertCondition("gmv_max", "GMV-Max sync failed", "boom2"))
        run_alert_check(db)        # fail again → email 3 (re-armed)
    assert len(sent) == 3


def test_disabled_is_noop(monkeypatch):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    monkeypatch.setattr(sa, "evaluate_sync_alerts",
                        lambda db: [AlertCondition("gmv_max", "x", "y")])
    # settings NOT configured → sync_alerts_enabled False
    with SessionLocal() as db:
        run_alert_check(db)
    assert sent == []


def test_send_failure_does_not_advance_state(monkeypatch, enabled):
    def boom(subject, body, *, to):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(sa.mailer, "send_email", boom)
    monkeypatch.setattr(sa, "evaluate_sync_alerts",
                        lambda db: [AlertCondition("gmv_max", "x", "y")])
    with SessionLocal() as db:
        run_alert_check(db)        # must not raise
        # state not advanced → no alerting row committed (retried next run)
        assert db.query(SyncAlert).filter_by(key="gmv_max", state="alerting").count() == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sync_alerts.py -v 2>&1 | tail -20`
Expected: FAIL — no module `app.services.sync_alerts`.

- [ ] **Step 3: Create the service**

```python
# app/services/sync_alerts.py
"""Turn current sync state into email alerts — edge-triggered, once-only.

evaluate_sync_alerts(db) reads the existing state (TikTokSyncState errors +
staleness, and the latest GMV-Max / SAP-inventory ImportBatch) into a list of
AlertConditions. run_alert_check(db) diffs that against the SyncAlert rows and
emails on the ok→alerting (failure) and alerting→ok (recovery) edges. Never
raises; a no-op when settings.sync_alerts_enabled is False. Called at the end of
the scheduler jobs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.import_batch import (
    ImportBatch, ImportBatchStatus, ImportFileKind, _utc_now_naive,
)
from app.models.sync_alert import SyncAlert
from app.models.tiktok_sync_state import TikTokSyncState
from app.services import mailer, tiktok_api, tiktok_sync

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertCondition:
    key: str
    title: str
    message: str


def _latest_batch_failed(db: Session, kind, key: str, title: str,
                         filename_prefix: str | None = None) -> list[AlertCondition]:
    """The most-recent matching ImportBatch decides: a FAILED latest batch is a
    condition, a non-FAILED latest batch clears it."""
    rows = db.execute(
        select(ImportBatch).where(ImportBatch.kind == kind)
        .order_by(desc(ImportBatch.id)).limit(10)
    ).scalars()
    for b in rows:
        if filename_prefix and not (b.original_filename or "").startswith(filename_prefix):
            continue
        if b.status == ImportBatchStatus.FAILED:
            return [AlertCondition(key, title, (b.error_message or "")[:500] or "import failed")]
        return []          # latest matching batch is healthy
    return []


def evaluate_sync_alerts(db: Session) -> list[AlertCondition]:
    out: list[AlertCondition] = []

    cred = tiktok_api.get_credential(db)
    if cred is not None and getattr(cred, "shop_cipher", None):
        states = db.query(TikTokSyncState).all()
        for s in states:
            if s.last_status == "error":
                out.append(AlertCondition(
                    key=f"tiktok:{s.stream}",
                    title=f"TikTok {s.stream} sync failed",
                    message=(s.last_message or "")[:500] or "sync error"))
        if settings.tiktok_auto_sync_enabled:
            runs = [s.last_run_at for s in states if s.last_run_at]
            if runs:
                hours = (_utc_now_naive() - max(runs)).total_seconds() / 3600
                if hours > tiktok_sync.STALE_HOURS:
                    out.append(AlertCondition(
                        key="tiktok:stale",
                        title="TikTok auto-sync is stale",
                        message=f"No sync run in {int(hours)}h (threshold {tiktok_sync.STALE_HOURS}h)."))

    out += _latest_batch_failed(db, ImportFileKind.TIKTOK_GMV_MAX, "gmv_max", "GMV-Max sync failed")
    out += _latest_batch_failed(db, ImportFileKind.INVENTORY_SNAPSHOT, "inventory",
                                "SAP inventory sync failed", filename_prefix="SAP")
    return out


def run_alert_check(db: Session) -> None:
    if not settings.sync_alerts_enabled:
        return

    active = {c.key: c for c in evaluate_sync_alerts(db)}
    existing = {row.key: row for row in db.query(SyncAlert).all()}
    now = _utc_now_naive()
    to = settings.sync_alert_to_list
    link = (settings.public_base_url or "").rstrip("/") + "/reports/recon-health"

    # ok → alerting (new failures)
    for key, cond in active.items():
        row = existing.get(key)
        if row is not None and row.state == "alerting":
            continue
        body = f"{cond.title}\n\n{cond.message}\n\nWhen: {now:%Y-%m-%d %H:%M} UTC\n{link}"
        try:
            mailer.send_email(f"⚠ Smashbox sync alert: {cond.title}", body, to=to)
        except Exception:  # noqa: BLE001
            logger.exception("sync alert email failed for %s", key)
            continue       # do NOT advance state — retried next run
        if row is None:
            row = SyncAlert(key=key)
            db.add(row)
        row.state = "alerting"
        row.message = cond.message
        row.last_transition_at = now

    # alerting → ok (recoveries)
    for key, row in existing.items():
        if row.state != "alerting" or key in active:
            continue
        body = f"{row.message or ''}\n\nRecovered at {now:%Y-%m-%d %H:%M} UTC\n{link}"
        try:
            mailer.send_email(f"✅ Smashbox sync recovered: {key}", body, to=to)
        except Exception:  # noqa: BLE001
            logger.exception("sync recovery email failed for %s", key)
            continue
        row.state = "ok"
        row.last_transition_at = now

    db.commit()
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_sync_alerts.py -v 2>&1 | tail -25`
Expected: 6 passed. (If `monkeypatch.setattr(settings, …)` is rejected by pydantic, the implementer may instead monkeypatch `sa.settings` with a small stand-in that has the needed attrs/properties — but prefer setattr; do not change production code.)

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sync alerts: evaluator + edge-triggered dispatcher

evaluate_sync_alerts reads TikTok stream errors/staleness + the latest GMV-Max
and SAP-inventory FAILED batches into AlertConditions. run_alert_check emails
once on ok→alerting and once on alerting→ok via the SyncAlert state row; never
raises; no-op when disabled; a send failure doesn't advance state (retried).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/services/sync_alerts.py tests/test_sync_alerts.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 5: Scheduler hook + manual test button

**Files:**
- Modify: `app/services/scheduler.py`
- Modify: `app/routers/uploads.py`
- Modify: `app/templates/uploads.html`
- Test: `tests/test_sync_alerts_button.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync_alerts_button.py
"""The scheduler runs the alert check after each job; the manual test button
sends a test email."""
import pytest
from fastapi.testclient import TestClient

import app.services.scheduler as sched
from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_tiktok_job_runs_alert_check(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.tiktok_api.get_credential", lambda db: None)
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check",
                        lambda db: calls.append("checked"))
    sched._run_tiktok_sync_job()       # not connected → returns early BUT alert check still runs
    # If the job returns before the alert check when not connected, this asserts the
    # check is still invoked; see Step 3 for placement.
    assert "checked" in calls


def test_inventory_job_runs_alert_check(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": None)
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max", lambda db: None)
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check",
                        lambda db: calls.append("checked"))
    sched._run_inventory_sync_job()
    assert "checked" in calls


def test_alert_check_failure_does_not_abort_job(monkeypatch):
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": None)
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max", lambda db: None)
    def boom(db):
        raise RuntimeError("alert check broke")
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check", boom)
    sched._run_inventory_sync_job()    # must NOT raise


def test_test_button_sends_email(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.mailer.send_email",
                        lambda subject, body, *, to: sent.append(subject))
    r = TestClient(app).post("/admin/sync-alerts/test", follow_redirects=False)
    assert r.status_code == 303
    assert sent and "test" in sent[0].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_sync_alerts_button.py -v 2>&1 | tail -20`
Expected: FAIL — alert check not called / route 404.

- [ ] **Step 3: Add the scheduler hook**

In `app/services/scheduler.py`, define a small helper and call it at the END of both jobs. Add the helper near the job functions:
```python
def _run_alert_check(db) -> None:
    """Evaluate sync health and fire/clear email alerts. Never raises."""
    try:
        from app.services.sync_alerts import run_alert_check
        run_alert_check(db)
    except Exception:  # noqa: BLE001
        logger.exception("sync alert check failed")
```

In `_run_tiktok_sync_job`, the current body returns early when not connected. Restructure so the alert check ALWAYS runs (GMV-Max/inventory failures are independent of the Shop connection). Make it:
```python
def _run_tiktok_sync_job() -> None:
    """Scheduler entry point: pull all TikTok streams if connected, then run the
    alert check. Own DB session; never propagate exceptions."""
    from app.services import tiktok_api, tiktok_sync

    with SessionLocal() as db:
        cred = tiktok_api.get_credential(db)
        if cred is None or not cred.shop_cipher:
            logger.info("tiktok auto-sync skipped — shop not connected")
        else:
            summary = tiktok_sync.run_sync(db, source="scheduled")
            logger.info("tiktok auto-sync complete: %s", summary)
        _run_alert_check(db)
```

In `_run_inventory_sync_job`, add `_run_alert_check(db)` as the last line inside the `with SessionLocal() as db:` block (after the SAP + GMV-Max syncs).

- [ ] **Step 4: Add the manual test route + button**

In `app/routers/uploads.py`, add the import (near the other auth/imports):
```python
from app.auth import require_admin
```
Add the route (near the other `@router.post` handlers):
```python
@router.post("/admin/sync-alerts/test", dependencies=[Depends(require_admin)])
async def sync_alerts_test(db: Session = Depends(get_db)):
    """Send a test alert email to verify SMTP config (admin only)."""
    from app.config import settings
    from app.services import mailer

    if not settings.sync_alerts_enabled:
        return RedirectResponse("/uploads?alert_test=disabled", status_code=303)
    try:
        await run_in_threadpool(
            mailer.send_email,
            "Smashbox sync alerts — test",
            "This is a test of the Smashbox sync-failure alerts. SMTP is configured correctly.",
            to=settings.sync_alert_to_list,
        )
        return RedirectResponse("/uploads?alert_test=sent", status_code=303)
    except Exception:  # noqa: BLE001
        logger.exception("sync alert test email failed")
        return RedirectResponse("/uploads?alert_test=failed", status_code=303)
```
(`logger` is already defined in uploads.py if it imports logging; if not, add `import logging` + `logger = logging.getLogger(__name__)` at the top.)

In `app/templates/uploads.html`, add a small card/button near the SAP + GMV-Max sync buttons:
```html
{# ── Sync failure alerts (test) ───────────────────────────────────────── #}
<div class="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
  <div class="flex items-start justify-between gap-3">
    <div>
      <h3 class="flex items-center gap-2 text-sm font-semibold text-slate-800">
        {{ ui.icon("triangle-alert", "h-4 w-4 text-slate-500") }}
        Sync failure alerts
      </h3>
      <p class="mt-0.5 text-xs text-slate-500">Emails you when a scheduled sync fails or recovers. Send a test to verify SMTP.</p>
    </div>
    <form action="/admin/sync-alerts/test" method="post">
      <button type="submit" class="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">Send test alert</button>
    </form>
  </div>
  {% if request.query_params.get('alert_test') == 'sent' %}
    <div class="mt-3 rounded bg-emerald-50 px-2 py-1 text-xs text-emerald-700">Test email sent.</div>
  {% elif request.query_params.get('alert_test') == 'failed' %}
    <div class="mt-3 rounded bg-rose-50 px-2 py-1 text-xs text-rose-700">Send failed — check SMTP config / logs.</div>
  {% elif request.query_params.get('alert_test') == 'disabled' %}
    <div class="mt-3 rounded bg-slate-100 px-2 py-1 text-xs text-slate-600">Alerts not configured (set the SMTP secrets).</div>
  {% endif %}
</div>
```
The `triangle-alert` icon is already committed (`app/static/icons/triangle-alert.svg`), so the icon-guard test passes with no vendoring needed.

- [ ] **Step 5: Run the tests + icon guard**

Run: `py -m pytest tests/test_sync_alerts_button.py -v 2>&1 | tail -20`
Expected: 4 passed.
Run: `py -m pytest -k "icon or scheduler or uploads" -q 2>&1 | tail -10`
Expected: pass (icon guard happy; scheduler tests still green).

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt`:
```
sync alerts: scheduler hook + manual test button

Both scheduled jobs run run_alert_check at the end (failure-isolated; the
TikTok job runs it even when not connected so GMV-Max/inventory failures still
alert). Add POST /admin/sync-alerts/test + an Uploads "Send test alert" button
to verify SMTP without waiting for a real failure.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/services/scheduler.py app/routers/uploads.py app/templates/uploads.html app/static/icons/ tests/test_sync_alerts_button.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 6: Full suite + deploy

**Files:** none (verification + ship)

- [ ] **Step 1: Full suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 835 + new tests; 11 skipped).

- [ ] **Step 2: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/sync-failure-alerts
git checkout main && git pull --ff-only
git merge --no-ff feature/sync-failure-alerts -m "Merge feature/sync-failure-alerts"
git push origin main
git branch -d feature/sync-failure-alerts && git push origin --delete feature/sync-failure-alerts
fly deploy
```
NOTE: this release's `alembic upgrade head` **runs the new `sync_alerts` migration** (creates the table) — NOT a no-op. The feature stays inert (`sync_alerts_enabled` False) until the SMTP secrets are set.

- [ ] **Step 3: Verify on prod**

`fly releases` healthy; `fly ssh console` confirm the `sync_alerts` table exists (`alembic current` = b1c2d3e4f5a6). The "Send test alert" button on `/uploads` returns the "not configured" notice until secrets are set.

- [ ] **Step 4: Hand SMTP setup to the user**

Tell the user to set the secrets themselves (do NOT ask for them in chat):
```
fly secrets set SMTP_HOST=smtp.gmail.com SMTP_PORT=587 \
  SMTP_USER=<addr> SMTP_PASSWORD=<gmail app password> \
  SYNC_ALERT_FROM=<addr> SYNC_ALERT_TO=<comma-separated recipients>
```
Then they click "Send test alert" on `/uploads` to confirm end-to-end.

---

## Self-Review

**Spec coverage:**
- Mailer (smtplib, STARTTLS, raises on failure) → Task 2. ✓
- Evaluator over TikTok streams + staleness + GMV-Max + SAP inventory → Task 4 (`evaluate_sync_alerts`, `_latest_batch_failed`). ✓
- Edge-triggered once-only failure + recovery via SyncAlert state → Task 4 (`run_alert_check`). ✓
- SyncAlert model + migration → Task 3. ✓
- Config SMTP settings + `sync_alert_to_list` + `sync_alerts_enabled` no-op gate → Task 1. ✓
- Scheduler hook in both jobs, failure-isolated → Task 5. ✓
- Manual test endpoint + button (admin) → Task 5. ✓
- Error handling: send failure caught, state not advanced, job unaffected → Task 4 + 5 (tests `test_send_failure_does_not_advance_state`, `test_alert_check_failure_does_not_abort_job`). ✓
- Known limitation (dead scheduler) documented in spec; not implemented (correct). ✓
- Out-of-scope (per-user prefs, SMS, thresholds, HTML email) honored. ✓

**Placeholder scan:** No TBD/TODO; every code step complete. The icon step has a concrete fallback. Pydantic-setattr test caveat noted with a fallback that doesn't touch prod code.

**Type consistency:** `AlertCondition(key,title,message)`, `evaluate_sync_alerts(db) -> list[AlertCondition]`, `run_alert_check(db) -> None`, `send_email(subject, body, *, to)`, `SyncAlert(key,state,message,last_transition_at,…)`, `settings.sync_alerts_enabled`/`sync_alert_to_list` are used identically across Tasks 1–5. The migration columns match the model. Scheduler calls `_run_alert_check(db)` → `run_alert_check`. ✓
