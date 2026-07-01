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
from app.services import mailer, tiktok_api

logger = logging.getLogger(__name__)


# Per-feed staleness thresholds. Daily feeds (Shop streams, ads, GMV-Max) alert
# past ~1.5 days; SAP inventory runs on weekdays, so its threshold spans a
# weekend gap to avoid a Monday-morning false alarm.
FEED_STALE_HOURS = 36
INVENTORY_STALE_HOURS = 80


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


# The 'ads' stream is authorized by the Marketing credential; every other stream
# by the Shop credential. Gating each on the right one means a Marketing failure
# alerts even when the Shop API is disconnected (and vice-versa).
_MARKETING_STREAMS = frozenset({"ads"})


def _shop_connected(db: Session) -> bool:
    c = tiktok_api.get_credential(db)
    return c is not None and bool(c.shop_cipher)


def _marketing_connected(db: Session) -> bool:
    from app.models.tiktok_marketing_credential import TikTokMarketingCredential
    c = db.query(TikTokMarketingCredential).order_by(TikTokMarketingCredential.id).first()
    return c is not None and bool(c.access_token)


def _latest_completed_batch_at(db: Session, kind, filename_prefix: str | None = None):
    """`completed_at` of the most-recent matching batch IF it COMPLETED — the
    "last success" time that drives staleness. Returns None when the latest
    matching batch failed (the FAILED-batch alert covers that) or none exists."""
    rows = db.execute(
        select(ImportBatch).where(ImportBatch.kind == kind)
        .order_by(desc(ImportBatch.id)).limit(10)
    ).scalars()
    for b in rows:
        if filename_prefix and not (b.original_filename or "").startswith(filename_prefix):
            continue
        return b.completed_at if b.status == ImportBatchStatus.COMPLETED else None
    return None


def _feed_staleness(db: Session) -> list[AlertCondition]:
    """Per-feed 'no successful sync in N hours' alerts, covering EVERY auto-synced
    feed. Each is gated on being both enabled (scheduled) and connected, so a
    single feed silently ceasing to run is caught — even when the others stay
    fresh (the old coarse max(last_run) check masked this) and even when no batch
    is FAILED (a stalled job writes no batch at all)."""
    from app.models.shop import Shop

    out: list[AlertCondition] = []
    now = _utc_now_naive()
    shop = db.query(Shop).order_by(Shop.id).first()

    shop_connected = _shop_connected(db)
    mkt_connected = _marketing_connected(db)

    states = {s.stream: s for s in db.query(TikTokSyncState).all()}

    def add(key: str, title: str, last_success, threshold_h: int, enabled: bool) -> None:
        if not enabled or last_success is None:
            return
        age_h = (now - last_success).total_seconds() / 3600
        if age_h > threshold_h:
            out.append(AlertCondition(
                key=f"stale:{key}", title=f"{title} sync is stale",
                message=f"No successful sync in {int(age_h)}h (threshold {threshold_h}h)."))

    # Shop streams — synced_through advances only on success.
    shop_enabled = shop_connected and settings.tiktok_auto_sync_enabled
    for stream, title in (("orders", "TikTok orders"), ("settlements", "TikTok settlements"),
                          ("payouts", "TikTok payouts"), ("analytics", "TikTok analytics")):
        st = states.get(stream)
        add(stream, title, st.synced_through if st else None, FEED_STALE_HOURS, shop_enabled)

    # Marketing feeds (ad-spend cost + GMV-Max) — share the gmv schedule + credential.
    mkt_enabled = mkt_connected and bool(shop and shop.gmv_sync_enabled)
    ads = states.get("ads")
    add("ads", "TikTok ad-spend", ads.synced_through if ads else None, FEED_STALE_HOURS, mkt_enabled)
    add("gmv_max", "GMV-Max",
        _latest_completed_batch_at(db, ImportFileKind.TIKTOK_GMV_MAX),
        FEED_STALE_HOURS, mkt_enabled)

    # SAP inventory — one batch writes both warehouses, so one staleness check.
    add("inventory", "SAP inventory",
        _latest_completed_batch_at(db, ImportFileKind.INVENTORY_SNAPSHOT, filename_prefix="SAP"),
        INVENTORY_STALE_HOURS, bool(shop and shop.inventory_sync_enabled))
    return out


def evaluate_sync_alerts(db: Session) -> list[AlertCondition]:
    out: list[AlertCondition] = []

    shop_connected = _shop_connected(db)
    mkt_connected = _marketing_connected(db)
    for s in db.query(TikTokSyncState).all():
        if s.last_status != "error":
            continue
        # Gate each stream on the credential that authorizes it, so a Marketing
        # failure isn't hidden by a disconnected Shop API (and vice-versa).
        connected = mkt_connected if s.stream in _MARKETING_STREAMS else shop_connected
        if connected:
            out.append(AlertCondition(
                key=f"tiktok:{s.stream}",
                title=f"TikTok {s.stream} sync failed",
                message=(s.last_message or "")[:500] or "sync error"))

    out += _feed_staleness(db)
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
