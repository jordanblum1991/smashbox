"""Pull daily GMV-Max metrics from TikTok's Marketing API instead of a manual
"Campaign overview By-Day" CSV upload.

Discovers the advertiser's GMV-Max campaigns + their store ids, pulls
`/gmv_max/report/get/` over a trailing window (chunked into <=30-day calls, the
API's max), aggregates the per-campaign/day rows into by-day totals, and feeds
them through the SAME writer as the CSV importer
(`gmv_max_campaign.import_dataframe`). Recorded as an `ImportBatch`
(kind TIKTOK_GMV_MAX) so it shows in Uploads history with a "last synced" time.
Idempotent: upsert by `metric_date`, so re-pulling recent (revised) days
overwrites in place.

Callable from the manual button (`routers/uploads.py`) and the weekday SAP
scheduler job (`services/scheduler.py`); both run it off the event loop. Never
raises — failures are recorded on the batch.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_t
from datetime import timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy.orm import Session

from app.importers.gmv_max_campaign import import_dataframe
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.services import tiktok_marketing_api as mapi

logger = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 30  # /gmv_max/report/get/ rejects ranges wider than this


class _SyncSkip(Exception):
    """Expected non-error stop (e.g. not connected) — recorded, not logged as a crash."""


def _date_chunks(start: date_t, end: date_t, max_days: int = MAX_WINDOW_DAYS):
    """Split [start, end] (inclusive) into consecutive <=max_days windows."""
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=max_days - 1))
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _aggregate(rows: list[dict]) -> pd.DataFrame:
    """Sum per-campaign/day report rows into one normalized row per day."""
    by_day: dict[date_t, dict] = defaultdict(
        lambda: {"cost": Decimal("0"), "sku_orders": 0, "gross_revenue": Decimal("0")})
    for r in rows:
        day = date_t.fromisoformat(r["stat_day"])
        agg = by_day[day]
        agg["cost"] += r["cost"]
        agg["sku_orders"] += int(r["orders"])
        agg["gross_revenue"] += r["gross_revenue"]
    return pd.DataFrame([
        {"metric_date": d, "cost": v["cost"], "sku_orders": v["sku_orders"],
         "gross_revenue": v["gross_revenue"]}
        for d, v in sorted(by_day.items())
    ])


def sync_gmv_max(db: Session, *, lookback_days: int = 35,
                 today: date_t | None = None) -> ImportBatch:
    """Pull the trailing `lookback_days` of GMV-Max metrics into
    GmvMaxDailyMetric. Returns the ImportBatch (COMPLETED or FAILED). Never
    raises: outcomes are recorded on the batch so the button/scheduler report
    cleanly."""
    today = today or date_t.today()
    start, end = today - timedelta(days=lookback_days), today
    ts = _utc_now_naive()

    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_GMV_MAX,
        status=ImportBatchStatus.PROCESSING,
        original_filename=f"TikTok GMV-Max API sync · {ts:%Y-%m-%d %H:%M}",
        stored_path="",
    )
    db.add(batch)
    db.flush()

    try:
        cred = mapi.get_credential(db)
        if cred is None or not cred.access_token:
            raise _SyncSkip("TikTok Marketing API not connected — connect it first.")

        token = cred.access_token
        all_rows: list[dict] = []
        found_campaigns = False
        for adv in mapi.advertiser_id_list(cred):
            campaigns = mapi.list_gmv_max_campaigns(token, adv)
            if not campaigns:
                continue
            found_campaigns = True
            store_ids = mapi.gmv_max_store_ids(token, adv, campaigns)
            if not store_ids:
                continue
            for chunk_start, chunk_end in _date_chunks(start, end):
                chunk_rows = mapi.get_gmv_max_report(
                    token, adv, store_ids,
                    chunk_start.isoformat(), chunk_end.isoformat())
                # Guard against the API returning rows outside the requested
                # window (can happen with stubs in tests; real TikTok responses
                # are bounded, but defensive filtering prevents double-counting
                # the same day across consecutive chunk calls).
                for row in chunk_rows:
                    row_day = date_t.fromisoformat(row["stat_day"])
                    if chunk_start <= row_day <= chunk_end:
                        all_rows.append(row)

        if not found_campaigns:
            batch.status = ImportBatchStatus.COMPLETED
            batch.rows_imported = 0
            batch.error_message = "No GMV-Max campaigns found for the connected advertiser(s)."
            batch.completed_at = _utc_now_naive()
            db.commit()
            return batch

        df = _aggregate(all_rows)
        res = import_dataframe(df, db, batch)
        note = (f"GMV-Max API sync: {res.rows_imported} days imported · "
                f"{res.rows_skipped} skipped · window {start}…{end}")
        batch.rows_imported = res.rows_imported
        batch.rows_skipped = res.rows_skipped
        batch.error_message = note + ("\n" + "\n".join(res.errors[:50]) if res.errors else "")
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = _utc_now_naive()
        db.commit()
        logger.info(note)
    except _SyncSkip as skip:
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = str(skip)
        batch.completed_at = _utc_now_naive()
        db.add(batch)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = f"GMV-Max API sync failed: {exc}"
        batch.completed_at = _utc_now_naive()
        db.add(batch)
        db.commit()
        logger.exception("GMV-Max API sync failed")

    return batch
