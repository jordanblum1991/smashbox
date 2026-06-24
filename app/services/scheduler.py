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
SALES_REPORT_JOB_ID = "sales_report_email"
SAMPLE_REPORT_JOB_ID = "sample_report_email"
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
            logger.warning("inventory report job fired but no primary shop found; skipping")
            return
        recipients = shop.report_recipients_list
        if not recipients:
            # Recipients were cleared after the job was registered (the apply gate
            # only checks at registration time). Skip quietly — this is a config
            # state, not a send failure, so do NOT trigger the failure alert.
            logger.warning("inventory report job fired with no recipients; skipping")
            return
        try:
            send_inventory_report(db, recipients=recipients)
            logger.info("inventory report emailed to %d recipient(s)", len(recipients))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled inventory report email failed")
            _alert_report_failure()


def _run_sales_report_job() -> None:
    """Scheduler entry point: email the Sales report for the configured rolling
    window. Own DB session; never propagates exceptions. On failure, log and (if
    the sync-alert channel is configured) fire a failure alert."""
    from app.services.report_email_common import resolve_rolling_period
    from app.services.reporting_tz import today_local
    from app.services.sales_report_email import send_sales_report

    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is None:
            logger.warning("sales report job fired but no primary shop found; skipping")
            return
        if not shop.sales_report_enabled:
            logger.warning("sales report job fired but disabled; skipping")
            return
        recipients = shop.sales_report_recipients_list
        if not recipients:
            logger.warning("sales report job fired with no recipients; skipping")
            return
        try:
            w = resolve_rolling_period(shop.sales_report_period, today=today_local())
            if w.fiscal_ym:
                send_sales_report(db, recipients=recipients, granularity="fiscal_month",
                                  start_date=None, end_date=None,
                                  year=w.fiscal_ym[0], month=w.fiscal_ym[1])
            else:
                send_sales_report(db, recipients=recipients, granularity="daily",
                                  start_date=w.start.isoformat(), end_date=w.end.isoformat(),
                                  year=None, month=None)
            logger.info("sales report emailed to %d recipient(s)", len(recipients))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled sales report email failed")
            _alert_report_failure("sales")


def _run_sample_report_job() -> None:
    """Scheduler entry point: email the Sample report for the configured rolling
    window. Own DB session; never propagates exceptions. On failure, log and (if
    the sync-alert channel is configured) fire a failure alert. Samples only
    offers month-level windows (prev_month/mtd), so the resolved window always
    maps to a single (year, month) via PeriodKind.MONTH."""
    from app.reports.pnl import PeriodKind
    from app.services.report_email_common import resolve_rolling_period
    from app.services.reporting_tz import today_local
    from app.services.sample_report_email import send_sample_report

    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is None:
            logger.warning("sample report job fired but no primary shop found; skipping")
            return
        if not shop.sample_report_enabled:
            logger.warning("sample report job fired but disabled; skipping")
            return
        recipients = shop.sample_report_recipients_list
        if not recipients:
            logger.warning("sample report job fired with no recipients; skipping")
            return
        try:
            w = resolve_rolling_period(shop.sample_report_period, today=today_local())
            send_sample_report(db, recipients=recipients, period=PeriodKind.MONTH,
                               year=w.start.year, month=w.start.month,
                               start_year=None, start_month=None,
                               end_year=None, end_month=None)
            logger.info("sample report emailed to %d recipient(s)", len(recipients))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled sample report email failed")
            _alert_report_failure("sample")


def _alert_report_failure(report: str = "inventory") -> None:
    """Best-effort failure alert via the existing sync-alert channel. `report`
    names which scheduled report email failed (inventory / sales / sample)."""
    try:
        from app.services import mailer
        if settings.sync_alerts_enabled:
            mailer.send_email(
                f"⚠ Smashbox {report} report failed",
                f"The scheduled {report} report email failed to send. "
                "Check the app logs for the exception detail.",
                to=settings.sync_alert_to_list,
            )
    except Exception:  # noqa: BLE001
        logger.exception("%s report failure-alert also failed", report)


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
        elif shop.inventory_report_enabled and not shop.report_recipients_list:
            logger.warning("inventory report email enabled but no recipients — not scheduled")
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


def apply_sales_report_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the Sales-report email job to match
    ``shop``. Thin wrapper over the generic register helper."""
    from app.services.report_email_common import register_report_job
    register_report_job(
        _scheduler, SALES_REPORT_JOB_ID,
        enabled=shop.sales_report_enabled,
        recipients=shop.sales_report_recipients_list,
        days=shop.sales_report_days,
        hour=shop.sales_report_hour,
        minute=shop.sales_report_minute,
        timezone=shop.timezone,
        run_fn=_run_sales_report_job,
    )


def apply_sample_report_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the Sample-report email job to match
    ``shop``. Thin wrapper over the generic register helper."""
    from app.services.report_email_common import register_report_job
    register_report_job(
        _scheduler, SAMPLE_REPORT_JOB_ID,
        enabled=shop.sample_report_enabled,
        recipients=shop.sample_report_recipients_list,
        days=shop.sample_report_days,
        hour=shop.sample_report_hour,
        minute=shop.sample_report_minute,
        timezone=shop.timezone,
        run_fn=_run_sample_report_job,
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
            apply_sales_report_schedule(shop)
            apply_sample_report_schedule(shop)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
