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

from app.config import settings
from app.db import SessionLocal
from app.models.shop import Shop

logger = logging.getLogger(__name__)

INVENTORY_JOB_ID = "inventory_sap_sync"
TIKTOK_JOB_ID = "tiktok_api_sync"

_scheduler: AsyncIOScheduler | None = None


def _run_inventory_sync_job() -> None:
    """Scheduler entry point: own DB session, never propagate exceptions (the
    sync service already records failures on an ImportBatch)."""
    from app.services.inventory_sync import sync_inventory_from_sap

    with SessionLocal() as db:
        sync_inventory_from_sap(db, source="scheduled")


def _run_tiktok_sync_job() -> None:
    """Scheduler entry point: pull all TikTok streams if the shop is connected.
    Own DB session; run_sync never raises (it records per-stream status). Skips
    cleanly when not yet connected, so the job can be registered up-front and
    starts working the moment the shop authorizes."""
    from app.services import tiktok_api, tiktok_sync

    with SessionLocal() as db:
        cred = tiktok_api.get_credential(db)
        if cred is None or not cred.shop_cipher:
            logger.info("tiktok auto-sync skipped — shop not connected")
            return
        summary = tiktok_sync.run_sync(db, source="scheduled")
        logger.info("tiktok auto-sync complete: %s", summary)


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
    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is not None:
            apply_inventory_schedule(shop)
            apply_tiktok_schedule(shop)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
