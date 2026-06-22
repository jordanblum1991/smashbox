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
        return []
    return []


def evaluate_sync_alerts(db: Session) -> list[AlertCondition]:
    out: list[AlertCondition] = []

    cred = tiktok_api.get_credential(db)
    if cred is not None and cred.shop_cipher:
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

    # Each alert key is independent: a send failure for one key does NOT roll back
    # another key's transition — only successfully-emailed transitions are committed.
    for key, cond in active.items():
        row = existing.get(key)
        if row is not None and row.state == "alerting":
            continue
        body = f"{cond.title}\n\n{cond.message}\n\nWhen: {now:%Y-%m-%d %H:%M} UTC\n{link}"
        try:
            mailer.send_email(f"⚠ Smashbox sync alert: {cond.title}", body, to=to)
        except Exception:  # noqa: BLE001
            logger.exception("sync alert email failed for %s", key)
            continue
        if row is None:
            row = SyncAlert(key=key)
            db.add(row)
        row.state = "alerting"
        row.message = cond.message
        row.last_transition_at = now

    for key, row in existing.items():
        if row.state != "alerting" or key in active:
            continue
        body = (f"{key} has recovered.\n\n"
                f"Previous failure: {row.message or 'n/a'}\n\n"
                f"Recovered at {now:%Y-%m-%d %H:%M} UTC\n{link}")
        try:
            mailer.send_email(f"✅ Smashbox sync recovered: {key}", body, to=to)
        except Exception:  # noqa: BLE001
            logger.exception("sync recovery email failed for %s", key)
            continue
        row.state = "ok"
        row.last_transition_at = now

    db.commit()
