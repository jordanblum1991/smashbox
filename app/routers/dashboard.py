"""Dashboard home — KPI tiles + recent imports."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.import_batch import ImportBatch
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.sample_tracking import monthly_sample_usage
from app.reports.unmapped_skus import find_unmapped_skus
from app.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    pnl = compute_monthly_pnl(db, today.year, today.month)
    samples = monthly_sample_usage(db, today.year, today.month)
    unmapped = find_unmapped_skus(db)
    recent = (
        db.query(ImportBatch)
        .order_by(ImportBatch.uploaded_at.desc())
        .limit(5)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "pnl": pnl,
            "samples": samples,
            "unmapped_count": len(unmapped),
            "recent": recent,
            "today": today,
        },
    )
