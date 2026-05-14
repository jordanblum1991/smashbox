"""HTML report views.

Each report renders to a Jinja template. Print styles live in static/css/app.css
so any report page can be sent to PDF or paper for brand meetings.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.reconciliation import reconcile_month
from app.reports.sample_tracking import (
    monthly_sample_usage,
    samples_vs_sales_by_sku,
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


@router.get("/reports/monthly-pnl")
def monthly_pnl_view(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    y, m = _ym(year, month)
    pnl = compute_monthly_pnl(db, y, m)
    return templates.TemplateResponse(
        request,
        "reports/monthly_pnl.html",
        {"pnl": pnl, "year": y, "month": m},
    )


@router.get("/reports/ytd-pnl")
def ytd_pnl_view(
    request: Request,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    y = year or date.today().year
    ytd = compute_ytd_pnl(db, y)
    return templates.TemplateResponse(
        request,
        "reports/ytd_pnl.html",
        {"ytd": ytd, "year": y},
    )


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
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    y, m = _ym(year, month)
    usage = monthly_sample_usage(db, y, m)
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    by_sku = samples_vs_sales_by_sku(db, start, end)
    return templates.TemplateResponse(
        request,
        "reports/sample_tracking.html",
        {"usage": usage, "by_sku": by_sku, "year": y, "month": m},
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
