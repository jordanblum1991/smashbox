"""Upload endpoint — accepts a file + kind, dispatches to the right importer."""
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import get_db
from app.importers import IMPORTERS
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.models.shop import Shop
from app.services.batch_deletion import delete_batch
from app.services.inventory_sync import sync_inventory_from_sap
from app.services.sample_classification import reconcile_sample_classifications
from app.services.scheduler import apply_inventory_schedule
from app.services.sku_resolver import resolve_all_order_lines
from app.templating import templates

# Valid APScheduler day_of_week tokens, in week order — guards the schedule form.
_VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# After these import kinds, run SKU resolution so newly-known SKUs / bundles
# back-fill existing OrderLine.unit_cogs_snapshot.
RESOLVE_AFTER = {
    ImportFileKind.TIKTOK_ORDERS,
    ImportFileKind.SKU_MASTER,
    ImportFileKind.BUNDLE_MAPPING,
}

# After these kinds, reconcile sample classification from settlement so
# free/paid-sample orders are excluded from GMV regardless of which file (orders
# or settlement) was imported first. See app/services/sample_classification.py.
RECONCILE_AFTER = {
    ImportFileKind.TIKTOK_ORDERS,
    ImportFileKind.TIKTOK_SETTLEMENTS,
}

router = APIRouter(tags=["uploads"])


def _run_import(db: Session, batch: ImportBatch, kind: ImportFileKind, stored_path) -> None:
    """Synchronous import work: run the importer, then (for catalog kinds) the
    SKU resolver, and commit. Runs in a worker thread via run_in_threadpool so a
    long import never blocks the event loop. Safe across threads: the SQLite
    engine sets check_same_thread=False and the caller awaits this (no concurrent
    Session access)."""
    importer_cls = IMPORTERS.get(kind)
    if importer_cls is None:
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = f"no importer registered for {kind.value}"
        db.commit()
        return

    try:
        result = importer_cls().run(stored_path, db, batch)
        batch.rows_imported = result.rows_imported
        batch.rows_skipped = result.rows_skipped
        batch.error_message = "\n".join(result.errors[:50]) or None
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = _utc_now_naive()

        if kind in RESOLVE_AFTER:
            stats = resolve_all_order_lines(db)
            tail = (
                f"resolved {stats.lines_resolved_sku} SKUs + "
                f"{stats.lines_resolved_bundle} bundles "
                f"(unresolved: {stats.lines_unresolved} of {stats.lines_inspected})"
            )
            batch.error_message = (
                f"{batch.error_message}\n{tail}" if batch.error_message else tail
            )

        if kind in RECONCILE_AFTER:
            reconcile_sample_classifications(db)

        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = str(exc)
        db.add(batch)
        db.commit()


@router.get("/uploads")
def uploads_page(request: Request, db: Session = Depends(get_db)):
    # Group batches by kind in the canonical ImportFileKind order so the
    # page renders one collapsible section per kind. Keeps the list scannable
    # once a project has accumulated many uploads.
    all_batches = (
        db.query(ImportBatch)
        .order_by(ImportBatch.uploaded_at.desc())
        .all()
    )
    batches_by_kind: dict[ImportFileKind, list[ImportBatch]] = {
        k: [] for k in ImportFileKind
    }
    for b in all_batches:
        batches_by_kind[b.kind].append(b)

    shop = db.query(Shop).order_by(Shop.id).first()
    # Last SAP sync = most recent inventory batch produced by the feed (its
    # filename starts with "SAP"), for the "last synced" line on the feed card.
    last_sap_sync = next(
        (b for b in batches_by_kind[ImportFileKind.INVENTORY_SNAPSHOT]
         if (b.original_filename or "").startswith("SAP")),
        None,
    )
    return templates.TemplateResponse(
        request,
        "uploads.html",
        {
            "batches_by_kind": batches_by_kind,
            "total_batches": len(all_batches),
            "kinds": list(ImportFileKind),
            "shop": shop,
            "last_sap_sync": last_sap_sync,
            "valid_days": _VALID_DAYS,
        },
    )


@router.post("/uploads/sync-inventory-sap")
async def sync_inventory_sap(db: Session = Depends(get_db)):
    """Manual 'Sync inventory from SAP' button. Runs the network + import work in
    a worker thread so the event loop isn't blocked, then returns to /uploads."""
    await run_in_threadpool(sync_inventory_from_sap, db, source="manual")
    return RedirectResponse("/uploads", status_code=303)


@router.post("/uploads/inventory-sync-settings")
def update_inventory_sync_settings(
    sync_time: str = Form("07:30"),
    enabled: str | None = Form(None),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the user-editable SAP auto-sync schedule on the Shop row and
    live-reschedule the running job. `enabled` is a checkbox (present only when
    ticked); `days` is the set of ticked weekday checkboxes; `sync_time` is the
    HH:MM from a native time input."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail="no shop configured")

    try:
        hh, mm = sync_time.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"bad time {sync_time!r}")

    chosen = [d for d in _VALID_DAYS if d in set(days)]  # validate + canonical order
    # No day selected = nothing to run, so treat as disabled rather than persist
    # an invalid empty cron expression.
    is_enabled = enabled is not None and bool(chosen)

    shop.inventory_sync_enabled = is_enabled
    shop.inventory_sync_hour = hour
    shop.inventory_sync_minute = minute
    if chosen:
        shop.inventory_sync_days = ",".join(chosen)
    db.commit()

    apply_inventory_schedule(shop)
    return RedirectResponse("/uploads", status_code=303)


@router.post("/uploads")
async def upload_file(
    kind: ImportFileKind = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    # Save the file first so we can re-run the importer later without re-uploading.
    stored_name = f"{_utc_now_naive():%Y%m%d_%H%M%S}_{file.filename}"
    stored_path = settings.upload_dir / stored_name
    with stored_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    batch = ImportBatch(
        kind=kind,
        status=ImportBatchStatus.PROCESSING,
        original_filename=file.filename or stored_name,
        stored_path=str(stored_path),
    )
    db.add(batch)
    db.flush()  # populate batch.id for child rows

    # Run the (synchronous, potentially minutes-long) import in a worker thread
    # so it never blocks the single event loop. Blocking it here froze the whole
    # app for the duration of an import — a real 16-min prod outage.
    await run_in_threadpool(_run_import, db, batch, kind, stored_path)
    return RedirectResponse("/uploads", status_code=303)


@router.post("/uploads/{batch_id}/delete")
def delete_batch_route(batch_id: int, db: Session = Depends(get_db)):
    """Roll back a single import batch.

    Behaviour depends on the batch's kind — see `app/services/batch_deletion.py`.
    Catalog batches (SKU_MASTER, BUNDLE_MAPPING) are audit-entry deletes only;
    the catalog rows themselves persist because Sku/Bundle have no
    import_batch_id column.
    """
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")

    try:
        delete_batch(db, batch)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return RedirectResponse("/uploads", status_code=303)
