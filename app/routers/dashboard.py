"""Dashboard home — KPI tiles + period-scoped detail tables.

Uses the same compute_pnl_view as /reports/pnl, so dashboard numbers
always tie to the P&L page for the selected period. The full import history
lives on /uploads; the dashboard only surfaces a small alert when the most
recent import failed.
"""
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.models.order import Order
from app.reports.dashboard_trends import build_dashboard_trends
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.seller_center_kpis import compute_seller_center_kpis
from app.reports.sample_tracking import (
    count_sample_orders_shipped,
    count_samples_shipped,
    samples_by_sku_shipped,
)
from app.reports.sku_profitability import compute_top_skus
from app.services.data_freshness import compute_freshness
from app.services.reporting_tz import today_local
from app.templating import strip_size, templates, title_case

router = APIRouter(tags=["dashboard"])


def _top_sku_view(r) -> dict:
    """Serialize a TopSkuRow for the dashboard's AG Grid (JSON-ready)."""
    return {
        "rank": r.rank,
        "tiktok_sku_id": r.tiktok_sku_id,
        "sku_code": r.sku_code,
        "name": (title_case(strip_size(r.name)) if r.name else None),
        "is_bundle": r.is_bundle,
        "is_unmapped": r.is_unmapped,
        "units_sold": r.units_sold,
        "net_customer_sales": float(r.net_customer_sales),
        "aov": float(r.aov),
    }


def _sample_sku_view(r) -> dict:
    """Serialize a ShippedSamplesBySkuRow for the dashboard's AG Grid."""
    return {
        "tiktok_sku_id": r.tiktok_sku_id,
        "sku_code": r.sku_code,
        "name": (title_case(strip_size(r.name)) if r.name else None),
        "is_bundle": r.is_bundle,
        "is_unmapped": r.is_unmapped,
        "samples_sent": r.samples_sent,
        "sample_orders_shipped": r.sample_orders_shipped,
        "units_sold": r.units_sold,
        "sold_per_sample": float(r.sold_per_sample),
    }


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
    # Seller Center (TikTok) KPI group — figures matched to Seller Center,
    # sourced from the Shop Analytics daily export (+ computed SKU orders).
    seller_center = compute_seller_center_kpis(db, start, end)
    top_skus = compute_top_skus(db, start, end, limit=10)
    samples_shipped = count_samples_shipped(db, start, end)
    sample_orders_shipped = count_sample_orders_shipped(db, start, end)
    # All-time lifetime total — period-independent, shown beside the period figure.
    # Wide window captures every sample regardless of the selected period.
    samples_shipped_all_time = count_samples_shipped(db, date(1970, 1, 1), date(2100, 1, 1))
    samples_by_sku = samples_by_sku_shipped(db, start, end)
    freshness = compute_freshness(db)

    # All-time ROAS — canonical: a full-range P&L view read with the same
    # `roas` definition (Net Customer Sales ÷ GROSS ad spend, before credits) as
    # the period figure, shown beside it in the same KPI box. The window is bounded to the actual
    # order-date range: compute_window_pnl ORs one clause per month touched, so
    # an open-ended span would blow SQLite's expression-depth limit.
    bounds = db.execute(
        select(func.min(Order.placed_at), func.max(Order.placed_at))
    ).one()
    if bounds[0] is not None and bounds[1] is not None:
        all_time_view = compute_pnl_view(
            db, PeriodKind.CUSTOM, None, None,
            start_date=bounds[0].date(), end_date=bounds[1].date() + timedelta(days=1),
        )
        roas_all_time = all_time_view.total.roas
        has_ads_all_time = all_time_view.total.total_ad_spend > 0
    else:
        roas_all_time = Decimal("0")
        has_ads_all_time = False

    # Trend affordances: trailing-6-month sparklines per headline KPI, plus a
    # MoM delta vs the previous calendar month. `end` is exclusive, so the last
    # included month is end - 1 day. Deltas only in single-month view (a MoM
    # delta on a multi-month aggregate would be apples-to-oranges); aggregate
    # views still get the sparkline trend.
    ref = end - timedelta(days=1)
    trends = build_dashboard_trends(
        db, ref.year, ref.month, with_delta=(view.period_kind == PeriodKind.MONTH)
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "view": view,
            "pnl": view.total,            # convenience for existing tile/waterfall code
            "samples_shipped": samples_shipped,
            "sample_orders_shipped": sample_orders_shipped,
            "samples_shipped_all_time": samples_shipped_all_time,
            "roas_all_time": roas_all_time,
            "has_ads_all_time": has_ads_all_time,
            "last_failed": last_failed,
            "today": today_local(),
            "top_skus": top_skus,
            "samples_by_sku": samples_by_sku,
            "top_skus_json": [_top_sku_view(r) for r in top_skus],
            "samples_json": [_sample_sku_view(r) for r in samples_by_sku],
            "freshness": freshness,
            "trends": trends,
            "seller_center": seller_center,
            "error": error,
        },
    )


def _dashboard_error_redirect(reason: str) -> RedirectResponse:
    """303 back to / with an error flash. Falls back to month mode so the
    user lands on a sensible default view. Modelled on _pnl_error_redirect
    in app/routers/reports.py."""
    qs = urlencode({"error": reason})
    return RedirectResponse(f"/?period=month&{qs}", status_code=303)
