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

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.importers.inventory_snapshot import import_dataframe
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)

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

    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"SAP feed returned {type(data).__name__}, expected a JSON list")
    return data


def sync_inventory_from_sap(
    db: Session, *, source: str = "manual", url: str | None = None,
    warehouse: str | None = None,
) -> ImportBatch:
    """Fetch the feed, import the sellable-warehouse rows, and return the
    ImportBatch (COMPLETED or FAILED). Never raises — failures are recorded on
    the batch so the caller (button or scheduler) can report cleanly."""
    url = url or settings.sap_inventory_url
    warehouse = warehouse or settings.sap_inventory_warehouse
    ts = _utc_now_naive()

    batch = ImportBatch(
        kind=ImportFileKind.INVENTORY_SNAPSHOT,
        status=ImportBatchStatus.PROCESSING,
        original_filename=f"SAP {warehouse} sync · {source} · {ts:%Y-%m-%d %H:%M}",
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

        sellable = [r for r in rows if str(r.get(_WAREHOUSE, "")).strip() == warehouse]
        df = pd.DataFrame({
            "sku": [str(r.get(_ITEMCODE, "")).strip() for r in sellable],
            "on_hand": [r.get(_ON_HAND) for r in sellable],
            "captured_at": [r.get(_DATE) for r in sellable],
        })

        result = import_dataframe(df, db, batch)

        note = (
            f"SAP sync ({source}): {len(rows)} feed rows, "
            f"{len(sellable)} in warehouse {warehouse}, "
            f"{result.rows_imported} imported, {result.rows_skipped} skipped"
        )
        batch.rows_imported = result.rows_imported
        batch.rows_skipped = result.rows_skipped
        batch.error_message = (
            note + ("\n" + "\n".join(result.errors[:50]) if result.errors else "")
        )
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
