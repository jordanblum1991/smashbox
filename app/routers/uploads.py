"""Upload endpoint — accepts a file + kind, dispatches to the right importer."""
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.importers import IMPORTERS
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.services.batch_deletion import delete_batch
from app.services.sku_resolver import resolve_all_order_lines
from app.templating import templates

# After these import kinds, run SKU resolution so newly-known SKUs / bundles
# back-fill existing OrderLine.unit_cogs_snapshot.
RESOLVE_AFTER = {
    ImportFileKind.TIKTOK_ORDERS,
    ImportFileKind.SKU_MASTER,
    ImportFileKind.BUNDLE_MAPPING,
}

router = APIRouter(tags=["uploads"])


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
    return templates.TemplateResponse(
        request,
        "uploads.html",
        {
            "batches_by_kind": batches_by_kind,
            "total_batches": len(all_batches),
            "kinds": list(ImportFileKind),
        },
    )


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

    importer_cls = IMPORTERS.get(kind)
    if importer_cls is None:
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = f"no importer registered for {kind.value}"
        db.commit()
        return RedirectResponse("/uploads", status_code=303)

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

        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = str(exc)
        db.add(batch)
        db.commit()

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
