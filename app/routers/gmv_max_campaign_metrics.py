"""Admin CRUD for manually-entered GMV Max campaign metrics.

Mirrors app/routers/gmv_max_reimbursements.py exactly in shape: month-keyed,
edit-not-stack (UNIQUE(year, month) → re-saving overwrites), delete by row id,
303-with-flash validation. Captures the two TikTok-reported, campaign-attributed
figures we can't derive from whole-shop data — Gross Revenue and SKU Orders.
Ad Cost is NOT entered here (it comes from the imported GMV-Max AdSpend); the
Ad Spend page derives Cost-per-Order and ROI from these plus that spend.
"""
import calendar
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 back to /admin/gmv-max-campaign-metrics with error/notice flash."""
    qs: dict[str, str] = {}
    if error:
        qs["error"] = error
    if notice:
        qs["notice"] = notice
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(f"/admin/gmv-max-campaign-metrics{suffix}", status_code=303)


@router.get(
    "/gmv-max-campaign-metrics",
    dependencies=[Depends(require_admin)],
)
def gmv_max_campaign_metrics_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    rows = db.execute(
        select(GmvMaxCampaignMetric).order_by(
            GmvMaxCampaignMetric.year.desc(),
            GmvMaxCampaignMetric.month.desc(),
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/gmv_max_campaign_metrics.html",
        {"rows": rows, "error": error, "notice": notice},
    )


@router.post(
    "/gmv-max-campaign-metrics",
    dependencies=[Depends(require_admin)],
)
def upsert_gmv_max_campaign_metric(
    year: str = Form(...),
    month: str = Form(...),
    gross_revenue: str = Form(...),
    sku_orders: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Upsert campaign metrics for (year, month). Re-saving the same month
    overwrites in place via the unique constraint."""
    try:
        y = int((year or "").strip())
    except ValueError:
        return _back(error=f"Year must be a number (got {year!r}).")
    try:
        m = int((month or "").strip())
    except ValueError:
        return _back(error=f"Month must be a number (got {month!r}).")
    if not (1 <= m <= 12):
        return _back(error=f"Month must be 1-12 (got {m}).")

    raw_gr = (gross_revenue or "").strip()
    if not raw_gr:
        return _back(error="Gross revenue is required.")
    try:
        gr = Decimal(raw_gr)
    except InvalidOperation:
        return _back(error=f"Gross revenue must be a number (got {raw_gr!r}).")
    if gr < 0:
        return _back(error=f"Gross revenue can't be negative (got {gr}).")

    raw_sku = (sku_orders or "").strip()
    if not raw_sku:
        return _back(error="SKU orders is required.")
    try:
        sku = int(raw_sku)
    except ValueError:
        return _back(error=f"SKU orders must be a whole number (got {raw_sku!r}).")
    if sku < 0:
        return _back(error=f"SKU orders can't be negative (got {sku}).")

    note_clean = (note or "").strip() or None
    row = db.execute(
        select(GmvMaxCampaignMetric).where(
            GmvMaxCampaignMetric.year == y,
            GmvMaxCampaignMetric.month == m,
        )
    ).scalar_one_or_none()
    is_new = row is None
    if is_new:
        row = GmvMaxCampaignMetric(year=y, month=m)
        db.add(row)
    row.gross_revenue = gr
    row.sku_orders = sku
    row.note = note_clean
    db.commit()
    verb = "Added" if is_new else "Updated"
    label = f"{calendar.month_name[m]} {y}"
    return _back(notice=f"{verb} campaign metrics for {label}.")


@router.post(
    "/gmv-max-campaign-metrics/{row_id}/delete",
    dependencies=[Depends(require_admin)],
)
def delete_gmv_max_campaign_metric(
    row_id: int,
    db: Session = Depends(get_db),
):
    row = db.get(GmvMaxCampaignMetric, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign metric not found")
    label = f"{calendar.month_name[row.month]} {row.year}"
    db.delete(row)
    db.commit()
    return _back(notice=f"Deleted campaign metrics for {label}.")
