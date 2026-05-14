"""HTML report views.

Each report renders to a Jinja template. Print styles live in static/css/app.css
so any report page can be sent to PDF or paper for brand meetings.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view
from app.reports.reconciliation import reconcile_month
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
    db: Session = Depends(get_db),
):
    """Unified P&L: pick a single month, a year, or YTD through a month."""
    today = date.today()
    y = year or today.year
    m = month or today.month
    view = compute_pnl_view(db, period, y, m)
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


@router.get("/reports/reconciliation")
def reconciliation_view(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    y, m = _ym(year, month)
    report = reconcile_month(db, y, m)
    return templates.TemplateResponse(
        request,
        "reports/reconciliation.html",
        {"report": report},
    )
