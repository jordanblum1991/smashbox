"""Pull on-hand inventory from the SAP feed instead of a manual CSV upload.

The feed (``settings.sap_inventory_url``) returns JSON rows shaped like::

    {"Itemcode": "SBX-C01101", "WhsCode": "SB", "OnHand": "364",
     "InventoryDate": "2026-06-12 09:40:54.203"}

We keep only the **SB** (sellable) warehouse — MIA is "missing inventory", 01 is
unused, and SBS is sample stock tracked separately — and feed those rows through
the SAME path as the CSV importer (`inventory_snapshot.import_dataframe`), so the
demand planner sees no difference in where the numbers came from.

Each sync is recorded as an `ImportBatch` (kind INVENTORY_SNAPSHOT) so it shows in
the Uploads history with a "last synced" time, and the raw response is saved to
the upload dir for audit. Idempotent: `captured_at` truncates to the feed date,
so a same-day re-sync (manual or scheduled) updates on-hand in place.

Callable from the manual button (`routers/uploads.py`) and the scheduler
(`services/scheduler.py`); both run it off the event loop.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.importers.inventory_snapshot import import_dataframe
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sample_inventory_snapshot import SampleInventorySnapshot

logger = logging.getLogger(__name__)

# Feed column -> normalized importer column.
_ITEMCODE = "Itemcode"
_WAREHOUSE = "WhsCode"
_ON_HAND = "OnHand"
_DATE = "InventoryDate"


def fetch_sap_inventory(url: str) -> list[dict]:
    """GET the feed and return the parsed JSON list. Isolated so tests can
    monkeypatch it with a fixture instead of hitting the network."""
    import httpx

    from app.services.http_retry import send_with_retry

    resp = send_with_retry(lambda: httpx.get(url, timeout=30.0), label="SAP inventory")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"SAP feed returned {type(data).__name__}, expected a JSON list")
    return data


def last_synced_at(db: Session, model) -> datetime | None:
    """When inventory of this kind was last refreshed: the completion time of the
    most-recent import that wrote a row to `model` (InventorySnapshot = sellable,
    SampleInventorySnapshot = sample). Full UTC-naive timestamp — the snapshot's
    own captured_at is truncated to date, so this is the precise 'last updated'
    moment. Falls back to uploaded_at when completed_at is null."""
    return db.execute(
        select(func.max(func.coalesce(ImportBatch.completed_at, ImportBatch.uploaded_at)))
        .select_from(model)
        .join(ImportBatch, ImportBatch.id == model.import_batch_id)
    ).scalar()


def _warehouse_frame(rows: list[dict], warehouse: str) -> pd.DataFrame:
    """Build the normalized (sku/on_hand/captured_at) frame for one warehouse."""
    kept = [r for r in rows if str(r.get(_WAREHOUSE, "")).strip() == warehouse]
    return pd.DataFrame({
        "sku": [str(r.get(_ITEMCODE, "")).strip() for r in kept],
        "on_hand": [r.get(_ON_HAND) for r in kept],
        "captured_at": [r.get(_DATE) for r in kept],
    })


def sync_inventory_from_sap(
    db: Session, *, source: str = "manual", url: str | None = None,
) -> ImportBatch:
    """Fetch the feed and import both warehouses in one batch — SB (sellable) →
    InventorySnapshot and SBS (sample pool) → SampleInventorySnapshot, kept in
    separate tables. Returns the ImportBatch (COMPLETED or FAILED). Never raises:
    failures are recorded on the batch so the caller (button or scheduler) can
    report cleanly. `batch.rows_imported` counts the sellable rows (the primary
    demand-planning import); the sample count is noted in the message."""
    url = url or settings.sap_inventory_url
    sellable_whs = settings.sap_inventory_warehouse
    sample_whs = settings.sap_sample_warehouse
    ts = _utc_now_naive()

    batch = ImportBatch(
        kind=ImportFileKind.INVENTORY_SNAPSHOT,
        status=ImportBatchStatus.PROCESSING,
        original_filename=f"SAP {sellable_whs}+{sample_whs} sync · {source} · {ts:%Y-%m-%d %H:%M}",
        stored_path="",
    )
    db.add(batch)
    db.flush()

    try:
        rows = fetch_sap_inventory(url)

        # Persist the raw response for audit / debugging.
        raw_path = settings.upload_dir / f"sap_inventory_{ts:%Y%m%d_%H%M%S}.json"
        raw_path.write_text(json.dumps(rows), encoding="utf-8")
        batch.stored_path = str(raw_path)

        sellable = import_dataframe(
            _warehouse_frame(rows, sellable_whs), db, batch, model=InventorySnapshot)
        sample = import_dataframe(
            _warehouse_frame(rows, sample_whs), db, batch, model=SampleInventorySnapshot)

        note = (
            f"SAP sync ({source}): {len(rows)} feed rows · "
            f"sellable {sellable_whs}: {sellable.rows_imported} imported · "
            f"sample {sample_whs}: {sample.rows_imported} imported · "
            f"{sellable.rows_skipped + sample.rows_skipped} skipped"
        )
        batch.rows_imported = sellable.rows_imported
        batch.rows_skipped = sellable.rows_skipped + sample.rows_skipped
        errors = sellable.errors + sample.errors
        batch.error_message = note + ("\n" + "\n".join(errors[:50]) if errors else "")
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = _utc_now_naive()
        db.commit()
        logger.info(note)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = f"SAP sync ({source}) failed: {exc}"
        batch.completed_at = _utc_now_naive()
        db.add(batch)
        db.commit()
        logger.exception("SAP inventory sync failed")

    return batch
