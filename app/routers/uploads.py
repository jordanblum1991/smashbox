"""Upload endpoint — accepts a file + kind, dispatches to the right importer."""
import shutil
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.importers import IMPORTERS
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.templating import templates

router = APIRouter(tags=["uploads"])


@router.get("/uploads")
def uploads_page(request: Request, db: Session = Depends(get_db)):
    batches = (
        db.query(ImportBatch)
        .order_by(ImportBatch.uploaded_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "uploads.html",
        {"batches": batches, "kinds": list(ImportFileKind)},
    )


@router.post("/uploads")
async def upload_file(
    kind: ImportFileKind = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    # Save the file first so we can re-run the importer later without re-uploading.
    stored_name = f"{datetime.utcnow():%Y%m%d_%H%M%S}_{file.filename}"
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
        batch.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        batch.status = ImportBatchStatus.FAILED
        batch.error_message = str(exc)
        db.add(batch)
        db.commit()

    return RedirectResponse("/uploads", status_code=303)
