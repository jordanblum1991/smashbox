"""Admin CRUD for Smashbox GMV Max reimbursements.

Parallel pipeline to AdCredit (which captures TikTok-issued credits). The
two flows are fully independent: separate model, separate route file,
separate template, separate aggregation field on MonthlyPnL. Neither
shadows nor interferes with the other.

Each entry is one Smashbox-confirmed reimbursement amount for a single
(year, month). UNIQUE(year, month) means re-saving the same month
overwrites in place; delete is by row id. Validation discipline mirrors
AdCredit: blank or unparseable input is REJECTED via 303 with ?error
flash. Amount must be > 0; null = not yet entered.
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
from app.models.gmv_max_reimbursement import GmvMaxReimbursement
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 back to /admin/gmv-max-reimbursements with error/notice flash."""
    qs: dict[str, str] = {}
    if error:
        qs["error"] = error
    if notice:
        qs["notice"] = notice
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(f"/admin/gmv-max-reimbursements{suffix}", status_code=303)


@router.get(
    "/gmv-max-reimbursements",
    dependencies=[Depends(require_admin)],
)
def gmv_max_reimbursements_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    rows = db.execute(
        select(GmvMaxReimbursement).order_by(
            GmvMaxReimbursement.year.desc(),
            GmvMaxReimbursement.month.desc(),
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/gmv_max_reimbursements.html",
        {"rows": rows, "error": error, "notice": notice},
    )


@router.post(
    "/gmv-max-reimbursements",
    dependencies=[Depends(require_admin)],
)
def upsert_gmv_max_reimbursement(
    year: str = Form(...),
    month: str = Form(...),
    amount: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Upsert a reimbursement for (year, month). Re-saving the same month
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
    raw_amount = (amount or "").strip()
    if not raw_amount:
        return _back(error="Amount is required.")
    try:
        amt = Decimal(raw_amount)
    except InvalidOperation:
        return _back(error=f"Amount must be a number (got {raw_amount!r}).")
    if amt <= 0:
        return _back(error=f"Amount must be greater than 0 (got {amt}).")
    note_clean = (note or "").strip() or None
    row = db.execute(
        select(GmvMaxReimbursement).where(
            GmvMaxReimbursement.year == y,
            GmvMaxReimbursement.month == m,
        )
    ).scalar_one_or_none()
    is_new = row is None
    if is_new:
        row = GmvMaxReimbursement(year=y, month=m)
        db.add(row)
    row.amount = amt
    row.note = note_clean
    db.commit()
    verb = "Added" if is_new else "Updated"
    label = f"{calendar.month_name[m]} {y}"
    return _back(notice=f"{verb} reimbursement for {label}: ${amt}.")


@router.post(
    "/gmv-max-reimbursements/{row_id}/delete",
    dependencies=[Depends(require_admin)],
)
def delete_gmv_max_reimbursement(
    row_id: int,
    db: Session = Depends(get_db),
):
    row = db.get(GmvMaxReimbursement, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Reimbursement not found")
    label = f"{calendar.month_name[row.month]} {row.year}"
    db.delete(row)
    db.commit()
    return _back(notice=f"Deleted reimbursement for {label}.")
