"""Dashboard home — KPI tiles + period-scoped detail tables.

Uses the same compute_pnl_view as /reports/pnl, so dashboard numbers
always tie to the P&L page for the selected period. The full import history
lives on /uploads; the dashboard only surfaces a small alert when the most
recent import failed.
"""
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.sample_tracking import count_samples_shipped, samples_by_sku_shipped
from app.reports.sku_profitability import compute_top_skus
from app.services.data_freshness import compute_freshness
from app.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/")
def home(
    request: Request,
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    start_date: str | None = None,    # ISO YYYY-MM-DD; CUSTOM mode only
    end_date: str | None = None,
    error: str | None = None,          # rendered as banner; set by _dashboard_error_redirect
    db: Session = Depends(get_db),
):
    # CUSTOM-range parse + validate mirrors /reports/pnl (see pnl_view in
    # app/routers/reports.py). Without this block compute_pnl_view raises
    # ValueError on missing dates and the dashboard 500s.
    sd_obj: date | None = None
    ed_obj: date | None = None
    if period == PeriodKind.CUSTOM:
        try:
            sd_obj = date.fromisoformat(start_date) if start_date else None
            ed_obj = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return _dashboard_error_redirect("Invalid date format — use YYYY-MM-DD.")
        if sd_obj is None or ed_obj is None:
            return _dashboard_error_redirect("Custom date range requires both start and end dates.")
        if sd_obj > ed_obj:
            return _dashboard_error_redirect("Start date must be on or before end date.")

    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
        start_date=sd_obj, end_date=ed_obj,
    )

    # Most recent failed import — surfaces a single alert at the top of the
    # Dashboard so a broken upload doesn't get lost when the user never visits
    # /uploads. None when the last batch is fine.
    last_failed = (
        db.query(ImportBatch)
        .filter(ImportBatch.status == ImportBatchStatus.FAILED)
        .order_by(ImportBatch.uploaded_at.desc())
        .first()
    )

    # Period-scoped extras — same window the P&L view uses.
    start, end = window_for(view)
    top_skus = compute_top_skus(db, start, end, limit=10)
    samples_shipped = count_samples_shipped(db, start, end)
    samples_by_sku = samples_by_sku_shipped(db, start, end)
    freshness = compute_freshness(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "view": view,
            "pnl": view.total,            # convenience for existing tile/waterfall code
            "samples_shipped": samples_shipped,
            "last_failed": last_failed,
            "today": date.today(),
            "top_skus": top_skus,
            "samples_by_sku": samples_by_sku,
            "freshness": freshness,
            "error": error,
        },
    )


def _dashboard_error_redirect(reason: str) -> RedirectResponse:
    """303 back to / with an error flash. Falls back to month mode so the
    user lands on a sensible default view. Modelled on _pnl_error_redirect
    in app/routers/reports.py."""
    qs = urlencode({"error": reason})
    return RedirectResponse(f"/?period=month&{qs}", status_code=303)
