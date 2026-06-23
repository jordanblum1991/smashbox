"""In-process APScheduler that runs the SAP inventory auto-sync.

Gated by two switches (see app/config.py):

  * ``settings.scheduler_enabled`` (env) — whether the scheduler runs at all.
    OFF by default so the test suite and local dev never spawn a background
    thread; production sets ``SCHEDULER_ENABLED=true``.
  * ``Shop.inventory_sync_enabled`` + hour/minute/days (DB, user-editable on the
    Uploads page) — whether and WHEN the sync job is registered. Editing the
    schedule there calls ``apply_inventory_schedule`` to live-reschedule.

The cron fires in the shop's own timezone (``Shop.timezone``). The single
always-warm Fly VM means it fires reliably; with exactly one app instance there
is no double-fire to guard against.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import SessionLocal
from app.models.import_batch import _utc_now_naive
from app.models.shop import Shop

logger = logging.getLogger(__name__)

INVENTORY_JOB_ID = "inventory_sap_sync"
TIKTOK_JOB_ID = "tiktok_api_sync"
HEARTBEAT_JOB_ID = "scheduler_heartbeat"
REPORT_JOB_ID = "inventory_report_email"
HEARTBEAT_INTERVAL_MIN = 15   # heartbeat cadence
HEARTBEAT_STALE_S = 3600      # /status/sync goes 503 past this age

_scheduler: AsyncIOScheduler | None = None
_heartbeat = None  # last proof-of-life, UTC-naive


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


def _run_alert_check(db) -> None:
    """Evaluate sync health and fire/clear email alerts. Never raises."""
    try:
        from app.services.sync_alerts import run_alert_check
        run_alert_check(db)
    except Exception:  # noqa: BLE001
        logger.exception("sync alert check failed")


def _run_inventory_sync_job() -> None:
    """Scheduler entry point: own DB session, never propagate exceptions. Runs the
    SAP inventory sync AND the GMV-Max API pull on the same weekday schedule; each
    is independent so one failing never aborts the other (both also record their
    own failures)."""
    from app.services.gmv_max_sync import sync_gmv_max
    from app.services.inventory_sync import sync_inventory_from_sap

    with SessionLocal() as db:
        try:
            sync_inventory_from_sap(db, source="scheduled")
        except Exception:  # noqa: BLE001
            logger.exception("scheduled SAP inventory sync failed")
        try:
            sync_gmv_max(db)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled GMV-Max sync failed")
        _run_alert_check(db)


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


def _run_inventory_report_job() -> None:
    """Scheduler entry point: email the weekly inventory report. Own DB session;
    never propagates exceptions. On failure, log and (if the sync-alert channel
    is configured) send a failure alert so the operator knows it didn't go out."""
    from app.services.inventory_report_email import send_inventory_report

    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is None:
            return
        try:
            send_inventory_report(db, recipients=shop.report_recipients_list)
            logger.info("inventory report emailed to %d recipient(s)",
                        len(shop.report_recipients_list))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled inventory report email failed")
            _alert_report_failure()


def _alert_report_failure() -> None:
    """Best-effort failure alert via the existing sync-alert channel."""
    try:
        from app.services import mailer
        if settings.sync_alerts_enabled:
            mailer.send_email(
                "⚠ Smashbox inventory report failed",
                "The scheduled weekly inventory report email failed to send. "
                "Check SMTP config and the app logs.",
                to=settings.sync_alert_to_list,
            )
    except Exception:  # noqa: BLE001
        logger.exception("inventory report failure-alert also failed")


def apply_tiktok_schedule(shop: Shop) -> None:
    """Register / remove the daily TikTok sync job in the shop's timezone. Safe
    to call when the scheduler isn't running (no-op)."""
    if _scheduler is None:
        return
    if not settings.tiktok_auto_sync_enabled:
        if _scheduler.get_job(TIKTOK_JOB_ID):
            _scheduler.remove_job(TIKTOK_JOB_ID)
            logger.info("tiktok auto-sync disabled — job removed")
        return

    trigger = CronTrigger(
        hour=settings.tiktok_sync_hour,
        minute=settings.tiktok_sync_minute,
        timezone=shop.timezone,  # string → apscheduler resolves via pytz
    )
    _scheduler.add_job(
        _run_tiktok_sync_job,
        trigger=trigger,
        id=TIKTOK_JOB_ID,
        replace_existing=True,
        coalesce=True,            # one run if several fire times were missed
        misfire_grace_time=3600,  # tolerate up to 1h late (e.g. after a restart)
        max_instances=1,
    )
    logger.info(
        "tiktok auto-sync scheduled: daily %02d:%02d %s",
        settings.tiktok_sync_hour, settings.tiktok_sync_minute, shop.timezone,
    )


def _primary_shop(db) -> Shop | None:
    return db.query(Shop).order_by(Shop.id).first()


def apply_inventory_report_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the weekly inventory-report email job to
    match ``shop``. Registered only when enabled AND recipients exist. Safe to
    call when the scheduler isn't running (no-op)."""
    if _scheduler is None:
        return

    if not (shop.inventory_report_enabled and shop.report_recipients_list):
        if _scheduler.get_job(REPORT_JOB_ID):
            _scheduler.remove_job(REPORT_JOB_ID)
            logger.info("inventory report email disabled — job removed")
        return

    trigger = CronTrigger(
        day_of_week=shop.inventory_report_days,
        hour=shop.inventory_report_hour,
        minute=shop.inventory_report_minute,
        timezone=shop.timezone,
    )
    _scheduler.add_job(
        _run_inventory_report_job,
        trigger=trigger,
        id=REPORT_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    logger.info(
        "inventory report email scheduled: %s %02d:%02d %s (%d recipients)",
        shop.inventory_report_days, shop.inventory_report_hour,
        shop.inventory_report_minute, shop.timezone,
        len(shop.report_recipients_list),
    )


def apply_inventory_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the inventory job to match ``shop``'s
    current schedule. Safe to call when the scheduler isn't running (no-op)."""
    if _scheduler is None:
        return

    if not shop.inventory_sync_enabled:
        if _scheduler.get_job(INVENTORY_JOB_ID):
            _scheduler.remove_job(INVENTORY_JOB_ID)
            logger.info("inventory auto-sync disabled — job removed")
        return

    trigger = CronTrigger(
        day_of_week=shop.inventory_sync_days,
        hour=shop.inventory_sync_hour,
        minute=shop.inventory_sync_minute,
        timezone=shop.timezone,  # string → apscheduler resolves via pytz
    )
    _scheduler.add_job(
        _run_inventory_sync_job,
        trigger=trigger,
        id=INVENTORY_JOB_ID,
        replace_existing=True,
        coalesce=True,            # one run if several fire times were missed
        misfire_grace_time=3600,  # tolerate up to 1h late (e.g. after a restart)
        max_instances=1,
    )
    logger.info(
        "inventory auto-sync scheduled: %s %02d:%02d %s",
        shop.inventory_sync_days, shop.inventory_sync_hour,
        shop.inventory_sync_minute, shop.timezone,
    )


def start_scheduler() -> None:
    """Start the scheduler (if enabled) and register the inventory job from the
    persisted shop schedule. Called from FastAPI startup."""
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED not set) — skipping")
        return
    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    record_heartbeat()
    _scheduler.add_job(
        record_heartbeat,
        trigger=IntervalTrigger(minutes=HEARTBEAT_INTERVAL_MIN),
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is not None:
            apply_inventory_schedule(shop)
            apply_tiktok_schedule(shop)
            apply_inventory_report_schedule(shop)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
