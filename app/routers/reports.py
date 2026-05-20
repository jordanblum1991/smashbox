"""HTML report views.

Each report renders to a Jinja template. Print styles live in static/css/app.css
so any report page can be sent to PDF or paper for brand meetings.
"""
from datetime import date, datetime

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.ad_credit import AdCredit
from app.models.import_batch import _utc_now_naive
from app.models.order import OrderLine
from app.reports.ad_spend import compute_ad_spend_summary
from app.reports.demand_planning import compute_demand_planning_view
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view
from app.reports.policy_violations import (
    compute_policy_violations,
    months_with_unacknowledged_violations,
)
from app.reports.reconciliation import (
    daily_sales_reconciliation,
    reconcile_month,
    yearly_sales_reconciliation,
)
from app.reports.sample_tracking import (
    SamplePeriodKind,
    compute_sample_view,
)
from app.reports.sku_profitability import compute_sku_profitability
from app.reports.settlement_only_orders import find_settlement_only_orders
from app.reports.unmapped_skus import find_unmapped_skus
from app.reports.ytd_pnl import compute_ytd_pnl
from app.templating import templates

router = APIRouter(tags=["reports"])


def _ym(year: int | None, month: int | None) -> tuple[int, int]:
    today = date.today()
    return year or today.year, month or today.month


@router.get("/reports/pnl")
def pnl_view(
    request: Request,
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    db: Session = Depends(get_db),
):
    """Unified P&L: pick a single month, YTD, full year, or a custom range."""
    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    return templates.TemplateResponse(
        request,
        "reports/pnl.html",
        {"view": view, "PeriodKind": PeriodKind},
    )


# Old URLs redirect to the unified page so bookmarks keep working.
@router.get("/reports/monthly-pnl")
def monthly_pnl_legacy(year: int | None = None, month: int | None = None):
    qs = f"period=month"
    if year: qs += f"&year={year}"
    if month: qs += f"&month={month}"
    return RedirectResponse(url=f"/reports/pnl?{qs}", status_code=307)


@router.get("/reports/ytd-pnl")
def ytd_pnl_legacy(year: int | None = None):
    qs = "period=year"
    if year: qs += f"&year={year}"
    return RedirectResponse(url=f"/reports/pnl?{qs}", status_code=307)


@router.get("/reports/sku-profitability")
def sku_profitability_view(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    y, m = _ym(year, month)
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    rows = compute_sku_profitability(db, start, end)
    return templates.TemplateResponse(
        request,
        "reports/sku_profitability.html",
        {"rows": rows, "year": y, "month": m},
    )


@router.get("/reports/samples")
def samples_view(
    request: Request,
    period: SamplePeriodKind = SamplePeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    db: Session = Depends(get_db),
):
    view = compute_sample_view(
        db,
        period,
        year=year, month=month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    return templates.TemplateResponse(
        request,
        "reports/sample_tracking.html",
        {"view": view, "SamplePeriodKind": SamplePeriodKind},
    )


@router.get("/reports/unmapped-skus")
def unmapped_skus_view(request: Request, db: Session = Depends(get_db)):
    rows = find_unmapped_skus(db)
    return templates.TemplateResponse(
        request,
        "reports/unmapped_skus.html",
        {"rows": rows},
    )


@router.get("/reports/settlement-only-orders")
def settlement_only_orders_view(request: Request, db: Session = Depends(get_db)):
    rows = find_settlement_only_orders(db)
    return templates.TemplateResponse(
        request,
        "reports/settlement_only_orders.html",
        {"rows": rows},
    )


@router.get("/reports/policy-violations")
def policy_violations_view(
    request: Request,
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    db: Session = Depends(get_db),
):
    view = compute_policy_violations(
        db, period,
        year=year, month=month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    pending_months = months_with_unacknowledged_violations(db)
    return templates.TemplateResponse(
        request,
        "reports/policy_violations.html",
        {
            "view": view,
            "PeriodKind": PeriodKind,
            "pending_months": pending_months,
        },
    )


@router.post("/reports/policy-violations/{order_line_id}/acknowledge")
def policy_violation_acknowledge(
    order_line_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Toggle the acknowledged flag on a flagged OrderLine. Acknowledged lines
    stay on the report (for audit) but stop counting toward the Data Health
    badge — see app/reports/policy_violations.count_policy_violations.

    Redirects back to wherever the form was submitted from so query-string
    state (period selector) survives the round trip."""
    line = db.get(OrderLine, order_line_id)
    if line is None or not line.discount_policy_violation:
        raise HTTPException(status_code=404, detail="flagged order line not found")
    line.policy_violation_acknowledged = not line.policy_violation_acknowledged
    line.policy_violation_acknowledged_at = (
        _utc_now_naive() if line.policy_violation_acknowledged else None
    )
    db.commit()
    redirect_to = request.headers.get("referer") or "/reports/policy-violations"
    return RedirectResponse(redirect_to, status_code=303)


@router.post("/reports/ad-spend/credits")
def upsert_ad_credit(
    year: int = Form(...),
    month: int = Form(...),
    amount: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Upsert a manual ad credit for (year, month).

    Empty/zero amount → row is deleted (so the P&L no longer offsets that
    month). Invalid input → silently treated as zero rather than 500ing; the
    user can re-submit.
    """
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1–12")
    try:
        amt = Decimal(amount.strip() or "0")
    except (InvalidOperation, AttributeError):
        amt = Decimal("0")
    amt = abs(amt)  # always store positive; we treat it as an offset magnitude
    note_clean = (note or "").strip() or None

    existing = db.execute(
        select(AdCredit).where(AdCredit.year == year, AdCredit.month == month)
    ).scalar_one_or_none()

    if amt == 0:
        if existing is not None:
            db.delete(existing)
    elif existing is None:
        db.add(AdCredit(year=year, month=month, amount=amt, note=note_clean))
    else:
        existing.amount = amt
        existing.note = note_clean
    db.commit()
    return RedirectResponse("/reports/ad-spend", status_code=303)


@router.get("/reports/demand-planning")
def demand_planning_view(
    request: Request,
    safety: str | None = None,
    cover: int | None = None,
    overstocked: int | None = None,
    db: Session = Depends(get_db),
):
    """Demand planning replenishment view.

    Query-string overrides: ?safety=0.10, ?cover=45, ?overstocked=180.
    `safety` is a fraction (0.10 = 10%, not "10"). Future: persistent settings UI.
    """
    safety_dec: Decimal | None = None
    if safety:
        try:
            safety_dec = Decimal(safety)
        except InvalidOperation:
            safety_dec = None

    # Buyer-supplied in-transit overrides come in as `receipts_<sku>=<n>` form
    # params on a POST; for the GET render we read from query string so links
    # can preserve them.
    expected_receipts: dict[str, int] = {}
    for k, v in request.query_params.items():
        if k.startswith("receipts_") and v.strip():
            try:
                expected_receipts[k[len("receipts_"):]] = int(v)
            except ValueError:
                pass

    view = compute_demand_planning_view(
        db,
        safety_stock_pct=safety_dec,
        cover_days=cover,
        overstocked_days=overstocked,
        expected_receipts=expected_receipts or None,
    )
    return templates.TemplateResponse(
        request,
        "reports/demand_planning.html",
        {"view": view},
    )


@router.get("/reports/ad-spend")
def ad_spend_view(request: Request, db: Session = Depends(get_db)):
    """Monthly TikTok ad spend with cash / credit / ad-credit breakdown."""
    summary = compute_ad_spend_summary(db)
    return templates.TemplateResponse(
        request,
        "reports/ad_spend.html",
        {"summary": summary, "today": date.today()},
    )


@router.get("/reports/reconciliation")
def reconciliation_view(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    y, m = _ym(year, month)
    report = reconcile_month(db, y, m)
    monthly_recon = yearly_sales_reconciliation(db, y)
    daily_recon = daily_sales_reconciliation(db, y, m)
    return templates.TemplateResponse(
        request,
        "reports/reconciliation.html",
        {
            "report": report,
            "monthly_recon": monthly_recon,
            "daily_recon": daily_recon,
        },
    )
