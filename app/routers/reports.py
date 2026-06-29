"""HTML report views.

Each report renders to a Jinja template. Print styles live in static/css/app.css
so any report page can be sent to PDF or paper for brand meetings.
"""
import calendar
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.ad_credit import AdCredit
from app.models.import_batch import _utc_now_naive
from app.models.order import OrderLine
from app.models.sku import Sku
from app.reports.ad_spend import (
    compute_ad_spend_daily,
    compute_ad_spend_fiscal,
    compute_ad_spend_monthly,
    compute_ad_spend_summary,
)
from app.reports.fiscal_calendar import fiscal_banner_payload
from app.reports.demand_planning import compute_demand_planning_view, compute_sku_detail_view
from app.reports.planner_accuracy import compute_planner_accuracy
from app.reports.dashboard_trends import (
    bar_chart,
    build_dashboard_trends,
    compute_delta,
    sparkline_points,
)
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.pnl_statement import available_fiscal_months
from app.reports.policy_violations import (
    all_policy_violations,
    compute_policy_violations,
    months_with_unacknowledged_violations,
)
from app.reports.reconciliation import (
    daily_sales_reconciliation,
    gmv_tie_out,
    reconcile_month,
    yearly_sales_reconciliation,
)
from app.reports.inventory_report import compute_inventory_report
from app.reports.sample_inventory import compute_sample_inventory_view
from app.reports.sample_tracking import (
    SamplePeriodKind,
    compute_sample_view,
    count_sample_orders_shipped,
    count_samples_shipped,
    samples_by_sku_shipped,
)
from app.reports.samples_by_creator import compute_samples_by_creator_view
from app.reports.settlement_only_orders import find_settlement_only_orders
from app.reports.unmapped_skus import find_unmapped_skus
from app.reports.ytd_pnl import compute_ytd_pnl
from app.reports.sales_report import (
    FISCAL_MODES, GRANULARITIES, compute_sales_report, current_fiscal_ym,
)
from app.auth import require_admin
from app.config import settings
from app.models.shop import Shop
from app.services.data_freshness import compute_freshness
from app.services.inventory_report_email import send_inventory_report
from app.services.report_email_common import (
    ROLLING_PERIODS, SALES_PERIODS, SAMPLE_PERIODS,
)
from app.services.reporting_tz import today_local
from app.services.sales_report_email import build_sales_csv, send_sales_report
from app.services.sample_report_email import send_sample_report
from app.services.scheduler import (
    apply_inventory_report_schedule,
    apply_sales_report_schedule,
    apply_sample_report_schedule,
)
from app.templating import strip_size, templates, title_case

router = APIRouter(tags=["reports"])

_REPORT_VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _ym(year: int | None, month: int | None) -> tuple[int, int]:
    today = today_local()
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
    start_date: str | None = None,    # ISO YYYY-MM-DD; CUSTOM mode only
    end_date: str | None = None,
    error: str | None = None,          # rendered as banner; set by _pnl_error_redirect
    db: Session = Depends(get_db),
):
    """Unified P&L: pick a single month, YTD, full year, a month range, or
    an arbitrary day range (CUSTOM)."""
    sd_obj: date | None = None
    ed_obj: date | None = None
    if period == PeriodKind.CUSTOM:
        try:
            sd_obj = date.fromisoformat(start_date) if start_date else None
            ed_obj = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return _pnl_error_redirect("Invalid date format — use YYYY-MM-DD.")
        if sd_obj is None or ed_obj is None:
            return _pnl_error_redirect("Custom date range requires both start and end dates.")
        if sd_obj > ed_obj:
            return _pnl_error_redirect("Start date must be on or before end date.")

    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
        start_date=sd_obj, end_date=ed_obj,
    )

    # Trend affordances on the period-summary tiles — mirrors the dashboard.
    # `end` is exclusive, so the last included month is end - 1 day. Deltas only
    # in single-month view (a MoM delta on a multi-month aggregate is
    # apples-to-oranges); sparklines render on every period kind.
    start, end = window_for(view)
    ref = end - timedelta(days=1)
    trends = build_dashboard_trends(
        db, ref.year, ref.month, with_delta=(period == PeriodKind.MONTH)
    )

    # Inline-SVG bar charts for the multi-month views (ytd/year/range), where a
    # real monthly series exists. Net Profit goes negative -> zero-baseline bar
    # chart; Net Customer Sales is the top-line companion. Geometry is computed
    # server-side; the template builds hover tooltips from the same months.
    charts = None
    if view.monthly_breakdown:
        bm = view.monthly_breakdown
        charts = {
            "net_profit": bar_chart([m.managed_net_profit for m in bm]),
            "net_customer_sales": bar_chart([m.managed_net_customer_sales for m in bm]),
        }

    return templates.TemplateResponse(
        request,
        "reports/pnl.html",
        {"view": view, "PeriodKind": PeriodKind, "trends": trends, "charts": charts,
         "error": error, "freshness": compute_freshness(db),
         "fiscal_banner": fiscal_banner_payload(view.period_kind.value, view.year, view.month)},
    )


@router.get("/reports/pnl/downloads")
def pnl_downloads(request: Request, db: Session = Depends(get_db)):
    """List every fiscal month with data, each linking to a per-month P&L
    CSV + PDF download (newest first)."""
    return templates.TemplateResponse(
        request, "reports/pnl_downloads.html",
        {"months": available_fiscal_months(db)},
    )


def _pnl_error_redirect(reason: str) -> RedirectResponse:
    """303 back to /reports/pnl with an error flash. Falls back to month
    mode so the user lands on a sensible default view. Modelled on
    _credit_error_redirect."""
    qs = urlencode({"error": reason})
    return RedirectResponse(f"/reports/pnl?period=month&{qs}", status_code=303)


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


def _sales_view_data(db, granularity, start_date, end_date, year, month):
    """Resolve a sales report + template context for the given query scope.
    Shared by the page and the CSV so they never diverge."""
    today = today_local()
    error = None
    fiscal_banner = fy = fm = None

    if granularity in FISCAL_MODES:
        cur_y, cur_m = current_fiscal_ym(today)
        fy = year if (year is not None and 2000 <= year <= 2100) else cur_y
        fm = month if (month is not None and 1 <= month <= 12) else cur_m
        if (year is not None and fy != year) or (month is not None and fm != month):
            error = "Invalid fiscal period — showing the current one."
        view = compute_sales_report(db, granularity, fiscal_year=fy, fiscal_month=fm)
        fiscal_banner = fiscal_banner_payload(granularity, fy, fm)
    else:
        if granularity not in GRANULARITIES:
            granularity = "daily"
        start = end = None
        if start_date and end_date:
            try:
                start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
                if start > end:
                    error, start, end = "Start date must be on or before end date.", None, None
            except ValueError:
                error, start, end = "Dates must be in YYYY-MM-DD format.", None, None
        view = compute_sales_report(db, granularity, start=start, end=end)

    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    cur_y, _ = current_fiscal_ym(today)
    return {
        "view": view, "granularities": GRANULARITIES, "granularity": granularity,
        "window_label": window_label,
        "chart": bar_chart([float(b.revenue) for b in view.buckets]),
        "start_date": start_date or "", "end_date": end_date or "",
        "fiscal_banner": fiscal_banner, "fiscal_year": fy, "fiscal_month": fm,
        "fiscal_years": list(range(cur_y - 2, cur_y + 1)), "error": error,
    }


PER_PAGE_OPTIONS = (10, 25, 50, 100)
DEFAULT_PER_PAGE = 25


@router.get("/reports/sales")
def sales_view(request: Request, granularity: str = "daily",
               start_date: str | None = None, end_date: str | None = None,
               year: int | None = None, month: int | None = None,
               tab: str = "overview", sort: str = "units", show_inactive: int = 0,
               per_page: int = DEFAULT_PER_PAGE, page: int = 1, dim: str = "dow",
               sent: str | None = None, err: str | None = None,
               db: Session = Depends(get_db)):
    """Sales report — Overview (velocity) or SKUs (per-SKU performance) tab, over
    the calendar/custom-range/fiscal period scopes. The SKU table is paginated."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    ctx["shop"] = db.query(Shop).order_by(Shop.id).first()
    ctx["valid_days"] = _REPORT_VALID_DAYS
    ctx["sales_periods"] = [(k, ROLLING_PERIODS[k]) for k in SALES_PERIODS]
    ctx["smtp_configured"] = bool(settings.smtp_host)
    ctx["flash_sent"] = sent
    ctx["flash_err"] = err
    ctx["tab"] = tab if tab in ("skus", "timing", "heatmap") else "overview"
    ctx["sort"] = sort
    ctx["show_inactive"] = bool(show_inactive)
    if ctx["tab"] == "skus":
        from app.reports.sku_performance import compute_sku_performance
        v = ctx["view"]
        sku = compute_sku_performance(db, start=v.window_start, end=v.window_end, sort=sort)
        ctx["sku"] = sku
        # Paginate the active rows (insights / % / totals stay over the full set).
        pp = per_page if per_page in PER_PAGE_OPTIONS else DEFAULT_PER_PAGE
        total = len(sku.rows)
        total_pages = max(1, -(-total // pp))         # ceil division
        pg = min(max(page, 1), total_pages)
        start_i = (pg - 1) * pp
        ctx["page_rows"] = sku.rows[start_i:start_i + pp]
        ctx["per_page"] = pp
        ctx["page"] = pg
        ctx["total_rows"] = total
        ctx["total_pages"] = total_pages
        ctx["per_page_options"] = PER_PAGE_OPTIONS
        ctx["row_start"] = start_i + 1 if total else 0
        ctx["row_end"] = min(start_i + pp, total)
        # Windowed page numbers (<=7), centered on the current page.
        if total_pages <= 7:
            ctx["page_window"] = list(range(1, total_pages + 1))
        else:
            lo = max(1, pg - 3)
            hi = min(total_pages, lo + 6)
            lo = max(1, hi - 6)
            ctx["page_window"] = list(range(lo, hi + 1))
    elif ctx["tab"] == "timing":
        from app.reports.temporal_patterns import compute_temporal_patterns
        from app.reports.dashboard_trends import bar_chart
        v = ctx["view"]
        t = compute_temporal_patterns(db, start=v.window_start, end=v.window_end)
        ctx["temporal"] = t
        ctx["dow_chart"] = bar_chart([float(d.avg_revenue) for d in t.dow])
        ctx["hour_chart"] = bar_chart([float(h.revenue) for h in t.hours])
        ctx["daily_chart"] = bar_chart([float(d.revenue) for d in t.daily])
    elif ctx["tab"] == "heatmap":
        from app.reports.sku_time_heatmap import compute_sku_time_heatmap
        v = ctx["view"]
        ctx["heatmap"] = compute_sku_time_heatmap(db, start=v.window_start, end=v.window_end, dim=dim)
        ctx["dim"] = ctx["heatmap"].dim
    return templates.TemplateResponse(request, "reports/sales.html", ctx)


@router.get("/reports/sales/sku/{sku_id}")
def sales_sku_detail_view(sku_id: str, request: Request,
                          granularity: str = "daily",
                          start_date: str | None = None, end_date: str | None = None,
                          year: int | None = None, month: int | None = None,
                          db: Session = Depends(get_db)):
    """Per-SKU sales drill-down over the on-screen period — performance row,
    12-week trend, recent orders, bundle membership. Cross-links to the
    demand-planner drill-down (the buying lens)."""
    from app.reports.sales_sku_detail import compute_sales_sku_detail
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    view = ctx["view"]
    detail = compute_sales_sku_detail(db, sku_id,
                                      start=view.window_start, end=view.window_end)
    period_qs = "granularity=" + granularity
    if ctx["start_date"] and ctx["end_date"]:
        period_qs += f"&start_date={ctx['start_date']}&end_date={ctx['end_date']}"
    if ctx["fiscal_year"]:
        period_qs += f"&year={ctx['fiscal_year']}&month={ctx['fiscal_month']}"
    return templates.TemplateResponse(request, "reports/sales_sku_detail.html",
                                      {"detail": detail, "window_label": ctx["window_label"],
                                       "period_qs": period_qs})


@router.post("/reports/sales/email-settings",
             dependencies=[Depends(require_admin)])
def update_sales_report_settings(
    recipients: str = Form(""),
    report_time: str = Form("08:00"),
    enabled: str | None = Form(None),
    period: str = Form("prev_month"),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the Sales-report email config and live-reschedule."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail="no shop configured")
    try:
        hh, mm = report_time.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"bad time {report_time!r}")

    chosen = [d for d in _REPORT_VALID_DAYS if d in set(days)]
    clean = [a.strip() for a in recipients.split(",") if a.strip()]
    period = period if period in SALES_PERIODS else "prev_month"
    shop.sales_report_recipients = ",".join(clean)
    shop.sales_report_period = period
    shop.sales_report_hour = hour
    shop.sales_report_minute = minute
    shop.sales_report_enabled = bool(enabled is not None and chosen and clean)
    if chosen:
        shop.sales_report_days = ",".join(chosen)
    db.commit()
    apply_sales_report_schedule(shop)
    return RedirectResponse("/reports/sales?sent=settings", status_code=303)


@router.post("/reports/sales/send-now",
             dependencies=[Depends(require_admin)])
def send_sales_report_now(
    granularity: str = Form("daily"),
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    year: int | None = Form(None),
    month: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """Email the Sales report immediately, covering the on-screen scope."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None or not shop.sales_report_recipients_list:
        return RedirectResponse("/reports/sales?err=no-recipients", status_code=303)
    try:
        send_sales_report(db, recipients=shop.sales_report_recipients_list,
                          granularity=granularity, start_date=start_date,
                          end_date=end_date, year=year, month=month)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/reports/sales?err=send-failed", status_code=303)
    return RedirectResponse("/reports/sales?sent=ok", status_code=303)


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
    sent: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
):
    view = compute_sample_view(
        db,
        period,
        year=year, month=month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    # Dashboard-style sample KPIs (same definitions): period units + distinct
    # orders, plus the all-time units total (period-independent wide window).
    sample_units_period = count_samples_shipped(db, view.start, view.end)
    sample_orders_period = count_sample_orders_shipped(db, view.start, view.end)
    sample_units_all_time = count_samples_shipped(db, datetime(1970, 1, 1), datetime(2100, 1, 1))
    # Comprehensive sample-by-SKU report (samples + orders), JSON for the AG Grid.
    sku_report = [
        {
            "sku_code": r.sku_code,
            "name": (title_case(strip_size(r.name)) if r.name else None),
            "tiktok_sku_id": r.tiktok_sku_id,
            "is_bundle": r.is_bundle,
            "is_unmapped": r.is_unmapped,
            "samples_sent": r.samples_sent,
            "sample_orders_shipped": r.sample_orders_shipped,
            "units_sold": r.units_sold,
            "sold_per_sample": float(r.sold_per_sample),
        }
        for r in samples_by_sku_shipped(db, view.start, view.end)
    ]
    return templates.TemplateResponse(
        request,
        "reports/sample_tracking.html",
        {
            "view": view,
            "SamplePeriodKind": SamplePeriodKind,
            "sample_units_period": sample_units_period,
            "sample_orders_period": sample_orders_period,
            "sample_units_all_time": sample_units_all_time,
            "sample_sku_rows": sku_report,
            "shop": db.query(Shop).order_by(Shop.id).first(),
            "valid_days": _REPORT_VALID_DAYS,
            "sample_periods": [(k, ROLLING_PERIODS[k]) for k in SAMPLE_PERIODS],
            "smtp_configured": bool(settings.smtp_host),
            "flash_sent": sent,
            "flash_err": err,
        },
    )


@router.post("/reports/samples/email-settings",
             dependencies=[Depends(require_admin)])
def update_sample_report_settings(
    recipients: str = Form(""),
    report_time: str = Form("08:00"),
    enabled: str | None = Form(None),
    period: str = Form("prev_month"),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the Sample-report email config and live-reschedule."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail="no shop configured")
    try:
        hh, mm = report_time.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"bad time {report_time!r}")

    chosen = [d for d in _REPORT_VALID_DAYS if d in set(days)]
    clean = [a.strip() for a in recipients.split(",") if a.strip()]
    period = period if period in SAMPLE_PERIODS else "prev_month"
    shop.sample_report_recipients = ",".join(clean)
    shop.sample_report_period = period
    shop.sample_report_hour = hour
    shop.sample_report_minute = minute
    shop.sample_report_enabled = bool(enabled is not None and chosen and clean)
    if chosen:
        shop.sample_report_days = ",".join(chosen)
    db.commit()
    apply_sample_report_schedule(shop)
    return RedirectResponse("/reports/samples?sent=settings", status_code=303)


@router.post("/reports/samples/send-now",
             dependencies=[Depends(require_admin)])
def send_sample_report_now(
    period: PeriodKind = Form(PeriodKind.MONTH),
    year: int | None = Form(None),
    month: int | None = Form(None),
    start_year: int | None = Form(None),
    start_month: int | None = Form(None),
    end_year: int | None = Form(None),
    end_month: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """Email the Sample report immediately, covering the on-screen scope.

    Uses the pnl PeriodKind scope (matching the /samples-by-sku.csv download) so
    the emailed report ties to the on-screen CSV for the same window."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None or not shop.sample_report_recipients_list:
        return RedirectResponse("/reports/samples?err=no-recipients", status_code=303)
    try:
        send_sample_report(db, recipients=shop.sample_report_recipients_list,
                           period=period, year=year, month=month,
                           start_year=start_year, start_month=start_month,
                           end_year=end_year, end_month=end_month)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/reports/samples?err=send-failed", status_code=303)
    return RedirectResponse("/reports/samples?sent=ok", status_code=303)


@router.get("/reports/sample-inventory")
def sample_inventory_view(request: Request, db: Session = Depends(get_db)):
    """Sample pool on-hand inventory, from the latest SAP SBS snapshot."""
    view = compute_sample_inventory_view(db)
    return templates.TemplateResponse(
        request,
        "reports/sample_inventory.html",
        {"view": view},
    )


@router.get("/reports/inventory")
def inventory_report_view(
    request: Request,
    sent: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
):
    """Complete inventory: every SKU with sellable (SB) + sample (SBS) on-hand,
    plus the admin email-settings panel."""
    view = compute_inventory_report(db)
    shop = db.query(Shop).order_by(Shop.id).first()
    return templates.TemplateResponse(
        request,
        "reports/inventory_report.html",
        {
            "view": view,
            "shop": shop,
            "valid_days": _REPORT_VALID_DAYS,
            "flash_sent": sent,
            "flash_err": err,
            "smtp_configured": bool(settings.smtp_host),
        },
    )


@router.post("/reports/inventory/email-settings",
             dependencies=[Depends(require_admin)])
def update_inventory_report_settings(
    recipients: str = Form(""),
    report_time: str = Form("08:00"),
    enabled: str | None = Form(None),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the weekly inventory-report email config and live-reschedule."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail="no shop configured")
    try:
        hh, mm = report_time.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"bad time {report_time!r}")

    chosen = [d for d in _REPORT_VALID_DAYS if d in set(days)]
    clean = [a.strip() for a in recipients.split(",") if a.strip()]
    shop.inventory_report_recipients = ",".join(clean)
    shop.inventory_report_enabled = bool(enabled is not None and chosen and clean)
    shop.inventory_report_hour = hour
    shop.inventory_report_minute = minute
    if chosen:
        shop.inventory_report_days = ",".join(chosen)
    db.commit()
    apply_inventory_report_schedule(shop)
    return RedirectResponse("/reports/inventory", status_code=303)


@router.post("/reports/inventory/send-now",
             dependencies=[Depends(require_admin)])
def send_inventory_report_now(db: Session = Depends(get_db)):
    """Email the inventory report immediately to the saved recipients."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None or not shop.report_recipients_list:
        return RedirectResponse("/reports/inventory?err=no-recipients", status_code=303)
    try:
        send_inventory_report(db, recipients=shop.report_recipients_list)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/reports/inventory?err=send-failed", status_code=303)
    return RedirectResponse("/reports/inventory?sent=ok", status_code=303)


@router.get("/action-center")
def action_center_view(request: Request, db: Session = Depends(get_db)):
    """Consolidated open-items hub — everything that needs attention in one place."""
    from app.reports.action_center import compute_action_center
    return templates.TemplateResponse(
        request,
        "action_center.html",
        {"view": compute_action_center(db)},
    )


def _creator_view(r) -> dict:
    return {
        "creator_handle": r.creator_handle,
        "creator_name": r.creator_name,
        "is_legacy": r.is_legacy,
        "platform": r.platform,
        "total_samples_sent": r.total_samples_sent,
        "distinct_sku_count": r.distinct_sku_count,
        "total_shipping_cost": (float(r.total_shipping_cost) if r.total_shipping_cost is not None else None),
        "first_shipped_at": (r.first_shipped_at.strftime("%Y-%m-%d") if r.first_shipped_at else None),
        "last_shipped_at": (r.last_shipped_at.strftime("%Y-%m-%d") if r.last_shipped_at else None),
    }


def _unmapped_view(r) -> dict:
    return {
        "identifier": r.identifier,
        "units": r.units,
        "paid_units": r.paid_units,
        "sample_units": r.sample_units,
        "gross": float(r.gross),
        "line_count": r.line_count,
        "first_seen": (r.first_seen.strftime("%Y-%m-%d") if r.first_seen else None),
        "last_seen": (r.last_seen.strftime("%Y-%m-%d") if r.last_seen else None),
    }


def _orphan_view(r) -> dict:
    return {
        "tiktok_order_id": r.tiktok_order_id,
        "statement_ids": r.statement_ids,
        "settlement_gross": float(r.settlement_gross),
        "settlement_fees": float(r.settlement_fees),
        "paid_date": (r.paid_date.strftime("%Y-%m-%d") if r.paid_date else None),
        "settled_date": (r.settled_date.strftime("%Y-%m-%d") if r.settled_date else None),
    }


@router.get("/reports/samples-by-creator")
def samples_by_creator_view(request: Request, db: Session = Depends(get_db)):
    """Samples sent, grouped by creator."""
    view = compute_samples_by_creator_view(db)
    return templates.TemplateResponse(
        request,
        "reports/samples_by_creator.html",
        {"view": view, "creator_rows": [_creator_view(r) for r in view.rows]},
    )


@router.get("/reports/recon-health")
def recon_health_view(
    request: Request,
    tab: str = "data-health",
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    """Consolidated "Recon & Health" page: the Data Health checks (unmapped
    SKUs, orphan orders, policy violations) and the monthly Reconciliation as
    two server-rendered tabs under one menu item. `tab` selects the panel;
    Recon honors year/month like the old standalone route."""
    active_tab = "recon" if tab == "recon" else "data-health"
    ctx: dict = {"active_tab": active_tab}
    if active_tab == "recon":
        from app.reports.coverage_gaps import compute_order_coverage
        y, m = _ym(year, month)
        ctx.update({
            "report": reconcile_month(db, y, m),
            "monthly_recon": yearly_sales_reconciliation(db, y),
            "daily_recon": daily_sales_reconciliation(db, y, m),
            "gmv_recon": gmv_tie_out(db, y),
            "coverage": compute_order_coverage(db),
        })
    else:
        from app.reports.missing_cogs import find_missing_cogs
        ctx.update({
            "unmapped_rows": [_unmapped_view(r) for r in find_unmapped_skus(db)],
            "orphan_rows": [_orphan_view(r) for r in find_settlement_only_orders(db)],
            "violations": all_policy_violations(db, only_unacknowledged=True),
            "missing_cogs_rows": find_missing_cogs(db),
        })
    return templates.TemplateResponse(request, "reports/recon_health.html", ctx)


@router.get("/reports/data-health")
def data_health_view():
    """Back-compat: Data Health is now the default tab of /reports/recon-health."""
    return RedirectResponse("/reports/recon-health", status_code=303)


# ---------------------------------------------------------------------------
# CSV downloads for the report pages (Ad Spend, Reconciliation, Data Health).
# csv.writer handles quoting; Response streams the in-memory string.
# ---------------------------------------------------------------------------
def _csv_response(rows, header: list, filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow(row)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/ad-spend.csv")
def ad_spend_csv(
    scope: str = "month", year: int | None = None, month: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """GMV-Max ad-spend KPIs as CSV (mirrors the Ad Spend table). Calendar (all
    months) by default; fiscal scopes export the 29th–28th rows for the period."""
    from app.models.gmv_max_daily_metric import GmvMaxDailyMetric

    fiscal_modes = {"fiscal_month": "month", "fiscal_ytd": "ytd", "fiscal_year": "year"}
    if scope in fiscal_modes:
        latest = db.execute(select(func.max(GmvMaxDailyMetric.metric_date))).scalar()
        y = year or (latest.year if latest else today_local().year)
        m = month or (latest.month if latest else today_local().month)
        monthly = compute_ad_spend_fiscal(db, y, m, fiscal_modes[scope])
        fname = f"ad_spend_{scope}_{y}-{m:02d}.csv"
    else:
        monthly = compute_ad_spend_monthly(db, None, None)
        fname = "ad_spend.csv"

    def rows():
        for r in monthly.rows:
            yield [
                r.year, "%02d" % r.month, f"{r.gross_spend:.2f}", f"{r.roas:.2f}",
                r.sku_orders if r.sku_orders is not None else "",
                f"{r.cost_per_order:.2f}" if r.cost_per_order is not None else "",
                f"{r.gross_revenue:.2f}" if r.gross_revenue is not None else "",
                f"{r.roi:.2f}" if r.roi is not None else "",
            ]

    return _csv_response(
        rows(),
        ["Year", "Month", "Gross Spend (GMV-Max)", "Blended ROAS", "SKU Orders",
         "Cost per Order", "Gross Revenue", "Attributed ROAS"],
        fname,
    )


@router.get("/reports/ad-spend-daily.csv")
def ad_spend_daily_csv(
    start_date: str | None = None, end_date: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Per-DAY GMV-Max ad-spend KPIs (campaign-attributed) for the range as CSV
    — mirrors the Daily scope of the Ad Spend page. Defaults to the last 30 days
    of available data when no range is given."""
    from app.models.gmv_max_daily_metric import GmvMaxDailyMetric

    try:
        sd = date.fromisoformat(start_date) if start_date else None
        ed = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        sd = ed = None
    if sd is None or ed is None:
        latest = db.execute(select(func.max(GmvMaxDailyMetric.metric_date))).scalar()
        if latest is not None:
            ed = ed or latest
            sd = sd or (ed - timedelta(days=29))
    if sd is None or ed is None:
        return _csv_response(iter([]), ["Date", "SKU Orders", "Cost per Order",
                                        "Gross Revenue", "Attributed ROAS", "Gross Spend (GMV-Max)"],
                             "ad_spend_daily.csv")
    if sd > ed:
        sd, ed = ed, sd
    view = compute_ad_spend_daily(db, sd, ed)

    def rows():
        for r in view.rows:
            yield [
                r.day.isoformat(), r.sku_orders,
                f"{r.cost_per_order:.2f}" if r.cost_per_order is not None else "",
                f"{r.gross_revenue:.2f}",
                f"{r.roi:.2f}" if r.roi is not None else "",
                f"{r.gross_spend:.2f}",
            ]

    return _csv_response(
        rows(),
        ["Date", "SKU Orders", "Cost per Order", "Gross Revenue", "Attributed ROAS",
         "Gross Spend (GMV-Max)"],
        f"ad_spend_daily_{sd.isoformat()}_to_{ed.isoformat()}.csv",
    )


@router.get("/reports/sales.csv")
def sales_csv(granularity: str = "daily",
              start_date: str | None = None, end_date: str | None = None,
              year: int | None = None, month: int | None = None,
              tab: str = "overview", sort: str = "units",
              db: Session = Depends(get_db)) -> Response:
    """Sales table as CSV, mirroring the on-screen scope AND tab. The Overview
    tab exports the velocity table; the SKUs tab exports the per-SKU
    performance table (so the SKU report is downloadable, not just Overview)."""
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    view = ctx["view"]

    if granularity in FISCAL_MODES:
        suffix = f"{granularity}_{ctx['fiscal_year']}"
    elif ctx["start_date"] and ctx["end_date"] and not ctx["error"]:
        suffix = f"{ctx['start_date']}_to_{ctx['end_date']}"
    else:
        suffix = view.granularity

    if tab == "skus":
        from app.reports.sku_performance import (
            SKU_CSV_HEADER, compute_sku_performance, sku_performance_csv_rows,
        )
        skuview = compute_sku_performance(
            db, start=view.window_start, end=view.window_end, sort=sort)
        return _csv_response(
            sku_performance_csv_rows(skuview), SKU_CSV_HEADER,
            f"sales_skus_{suffix}.csv",
        )

    return Response(
        content=build_sales_csv(view),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="sales_{suffix}.csv"'},
    )


@router.get("/reports/reconciliation.csv")
def reconciliation_csv(
    year: int | None = None, month: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Daily sales reconciliation (our GMV vs TikTok GMV) for the month as CSV."""
    y, m = _ym(year, month)
    data = daily_sales_reconciliation(db, y, m)

    def rows():
        for r in data:
            yield [
                r.day.isoformat(), f"{r.gmv:.2f}",
                f"{r.tiktok_gmv:.2f}" if r.tiktok_gmv is not None else "",
                f"{r.tiktok_variance:.2f}" if r.tiktok_variance is not None else "",
                f"{r.refunds:.2f}", f"{r.net_customer_sales:.2f}", r.orders_count,
            ]

    return _csv_response(
        rows(),
        ["Date", "Our GMV", "TikTok GMV", "Variance", "Refunds",
         "Net Customer Sales", "Orders"],
        f"reconciliation_{y}-{m:02d}.csv",
    )


@router.get("/reports/data-health.csv")
def data_health_csv(db: Session = Depends(get_db)) -> Response:
    """All open data-quality issues (unmapped SKUs, orphan orders, policy
    violations, missing COGS) in one CSV, tagged by Issue Type."""
    from app.reports.missing_cogs import find_missing_cogs

    def rows():
        for r in find_unmapped_skus(db):
            yield ["Unmapped SKU", r.identifier,
                   f"{r.units} units, {r.line_count} lines",
                   f"{float(r.gross):.2f}",
                   r.last_seen.strftime("%Y-%m-%d") if r.last_seen else ""]
        for r in find_settlement_only_orders(db):
            sids = r.statement_ids
            sids_str = "; ".join(sids) if isinstance(sids, (list, tuple)) else (str(sids) if sids else "")
            yield ["Orphan Order", r.tiktok_order_id,
                   ("statements: " + sids_str) if sids_str else "",
                   f"{float(r.settlement_gross):.2f}",
                   r.settled_date.strftime("%Y-%m-%d") if r.settled_date else ""]
        for r in all_policy_violations(db, only_unacknowledged=True):
            yield ["Policy Violation", r.sku_code or r.sku,
                   f"order {r.tiktok_order_id}, excess {r.excess:.2f}",
                   f"{r.seller_funded_discount:.2f}",
                   r.placed_at.strftime("%Y-%m-%d")]
        for r in find_missing_cogs(db):
            yield ["Missing COGS", r.sku_code,
                   f"{r.name or ''} (on hand {r.on_hand}, sold {r.units_sold})".strip(),
                   "0.00", ""]

    return _csv_response(
        rows(),
        ["Issue Type", "Identifier", "Detail", "Amount", "Date"],
        "data_health.csv",
    )


@router.get("/reports/unmapped-skus")
def unmapped_skus_view(request: Request, db: Session = Depends(get_db)):
    rows = find_unmapped_skus(db)
    return templates.TemplateResponse(
        request,
        "reports/unmapped_skus.html",
        {"rows": rows, "unmapped_rows": [_unmapped_view(r) for r in rows]},
    )


@router.get("/reports/settlement-only-orders")
def settlement_only_orders_view(request: Request, db: Session = Depends(get_db)):
    rows = find_settlement_only_orders(db)
    return templates.TemplateResponse(
        request,
        "reports/settlement_only_orders.html",
        {"rows": rows, "orphan_rows": [_orphan_view(r) for r in rows]},
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
    applied_date: str = Form(...),
    amount: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Upsert a manual ad credit on a specific calendar date.

    The credit is keyed by (year, month) — at most one credit per calendar
    month, but the date inside that month can be any day. Re-saving for the
    same (year, month) updates in place, including re-specifying the date.

    Saving any amount — including $0 — records a confirmed entry that persists
    across reloads. There is no "clear" action; a saved $0 means "explicitly
    confirmed no credit this month" and is distinct from a month that was
    never touched. Unparseable or blank input → redirect back with an error
    query param; nothing is written.
    """
    raw_date = (applied_date or "").strip()
    if not raw_date:
        return _credit_error_redirect(None, "applied date is required (use YYYY-MM-DD)")
    try:
        ad_date = date.fromisoformat(raw_date)
    except ValueError:
        return _credit_error_redirect(
            None, f"{raw_date!r} is not a valid date (use YYYY-MM-DD)"
        )

    raw = (amount or "").strip()
    if not raw:
        return _credit_error_redirect(
            ad_date, "amount is required (enter 0 to confirm no credit)"
        )
    try:
        amt = abs(Decimal(raw))  # store positive; offset magnitude
    except InvalidOperation:
        return _credit_error_redirect(ad_date, f"{raw!r} is not a valid number")

    note_clean = (note or "").strip() or None

    # year/month are derived from applied_date so the legacy UNIQUE constraint
    # continues to enforce "one credit per calendar month".
    year, month = ad_date.year, ad_date.month
    existing = db.execute(
        select(AdCredit).where(AdCredit.year == year, AdCredit.month == month)
    ).scalar_one_or_none()
    if existing is None:
        db.add(AdCredit(
            year=year, month=month, applied_date=ad_date,
            amount=amt, note=note_clean,
        ))
    else:
        existing.applied_date = ad_date
        existing.amount = amt
        existing.note = note_clean
    db.commit()
    return RedirectResponse("/reports/ad-spend/reimbursements", status_code=303)


def _credit_error_redirect(ad_date: date | None, reason: str) -> RedirectResponse:
    """Build a 303 back to /reports/ad-spend with a query-param error flash.

    Modelled on app/routers/admin.py:_back — query params (not session flash)
    so the message survives the 303 cleanly without extra session machinery.
    `ad_date` is None when the date itself was the thing that failed to parse.
    """
    if ad_date is None:
        msg = f"Could not save credit: {reason}"
    else:
        label = f"{calendar.month_name[ad_date.month]} {ad_date.day}, {ad_date.year}"
        msg = f"Could not save credit for {label}: {reason}"
    qs = urlencode({"error": msg})
    return RedirectResponse(f"/reports/ad-spend/reimbursements?{qs}", status_code=303)


# Whitelist of columns the planner table can sort by. Anything else falls
# back to the report's native urgency-first sort.
_DEMAND_SORT_KEYS = {"on_hand", "days_of_supply", "stockout_date", "suggested_qty"}


def _apply_planner_sort(rows, sort_key: str, direction: str) -> None:
    """Sort `rows` in-place by the chosen column. Null dates / days-of-supply
    sink to the bottom regardless of direction (null = no signal, not a value)."""
    reverse = direction == "desc"
    if sort_key == "on_hand":
        rows.sort(key=lambda r: r.on_hand, reverse=reverse)
    elif sort_key == "suggested_qty":
        rows.sort(key=lambda r: r.suggested_order_qty, reverse=reverse)
    elif sort_key == "days_of_supply":
        rows.sort(key=lambda r: (
            1 if r.days_of_supply is None else 0,
            (-r.days_of_supply if reverse else r.days_of_supply)
            if r.days_of_supply is not None else Decimal("0"),
        ))
    elif sort_key == "stockout_date":
        # date.toordinal sorts dates as integers — negate for desc so nulls
        # still sink to the bottom rather than flipping to the top.
        rows.sort(key=lambda r: (
            1 if r.stockout_date is None else 0,
            (-r.stockout_date.toordinal() if reverse else r.stockout_date.toordinal())
            if r.stockout_date is not None else 0,
        ))


def _build_sort_links(request: Request, current_key: str | None, current_dir: str) -> dict:
    """Return a per-column `{href, is_active, arrow}` map the template uses
    to render the sortable headers. Preserves any other query params on the
    URL so safety/cover/receipts overrides survive a sort click."""
    from urllib.parse import urlencode

    base_params = [
        (k, v) for k, v in request.query_params.items() if k not in ("sort", "dir")
    ]

    def link_for(key: str) -> dict:
        is_active = current_key == key
        # Toggle direction on re-click; otherwise default to ascending.
        next_dir = "desc" if (is_active and current_dir == "asc") else "asc"
        params = base_params + [("sort", key), ("dir", next_dir)]
        href = "?" + urlencode(params)
        if not is_active:
            arrow = ""
        elif current_dir == "asc":
            arrow = "▲"
        else:
            arrow = "▼"
        return {"href": href, "is_active": is_active, "arrow": arrow}

    return {k: link_for(k) for k in _DEMAND_SORT_KEYS}


def _dp_row_view(r) -> dict:
    """Serialize a ReplenishmentResult for the demand-planning AG Grid."""
    return {
        "component_sku": r.component_sku,
        "sku_code": r.sku_code,
        "name": (title_case(strip_size(r.name)) if r.name else None),
        "status": r.status.value,
        "on_hand": r.on_hand,
        "expected_receipts": r.expected_receipts,
        "daily_velocity": float(r.daily_velocity),
        "daily_velocity_14d": float(r.daily_velocity_14d),
        "trend_ratio": float(r.trend_ratio),
        "days_of_supply": (float(r.days_of_supply) if r.days_of_supply is not None else None),
        "stockout_label": (r.stockout_date.strftime("%b %d") if r.stockout_date else None),
        "stockout_sort": (r.stockout_date.isoformat() if r.stockout_date else None),
        "lead_time_days": r.lead_time_days,
        "reorder_point": r.reorder_point,
        "suggested_order_qty": r.suggested_order_qty,
        "investment": float(r.investment),
    }


def _dp_pipeline_view(i) -> dict:
    """Serialize a PipelineItem for the demand-planning pipeline AG Grid."""
    return {
        "component_sku": i.component_sku,
        "sku_code": i.sku_code,
        "name": (title_case(strip_size(i.name)) if i.name else None),
        "status": i.status.value,
        "on_hand": i.on_hand,
        "daily_velocity": float(i.daily_velocity),
        "lead_time_days": i.lead_time_days,
        "days_until_reorder": i.days_until_reorder,
        "order_by_label": i.order_by_date.strftime("%b %d"),
        "order_by_sort": i.order_by_date.isoformat(),
        "suggested_qty": i.suggested_qty,
        "investment": float(i.investment),
    }


@router.get("/reports/planner-accuracy")
def planner_accuracy_view(request: Request, db: Session = Depends(get_db)):
    """Backtest of the demand planner — predicted vs actual demand, with a
    safety-stock / cover-days read. (See app/reports/planner_accuracy.py.)"""
    view = compute_planner_accuracy(db)
    return templates.TemplateResponse(
        request, "reports/planner_accuracy.html", {"view": view},
    )


@router.get("/reports/demand-planning")
def demand_planning_view(
    request: Request,
    service_level: str | None = None,
    cover: int | None = None,
    overstocked: int | None = None,
    sort: str | None = None,
    dir: str | None = None,
    db: Session = Depends(get_db),
):
    """Demand planning replenishment view.

    Query-string overrides:
      ?service_level=0.90|0.95|0.975  — drives the z in z × σ × √L (variance method)
      ?cover=45                        — forward-cover days
      ?overstocked=180                 — days_of_supply threshold for OVERSTOCKED
    Sort: ?sort=on_hand|days_of_supply|stockout_date|suggested_qty, ?dir=asc|desc.
    """
    sl_dec: Decimal | None = None
    if service_level:
        from app.config import SERVICE_LEVEL_Z_TABLE
        try:
            cand = Decimal(service_level)
            if cand in SERVICE_LEVEL_Z_TABLE:
                sl_dec = cand
        except InvalidOperation:
            sl_dec = None

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
        service_level_override=sl_dec,
        cover_days=cover,
        overstocked_days=overstocked,
        expected_receipts=expected_receipts or None,
    )

    sort_key = sort if sort in _DEMAND_SORT_KEYS else None
    sort_dir = "desc" if (dir or "").lower() == "desc" else "asc"
    if sort_key:
        _apply_planner_sort(view.rows, sort_key, sort_dir)

    from app.reports.in_transit import in_transit_summary

    return templates.TemplateResponse(
        request,
        "reports/demand_planning.html",
        {
            "view": view,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
            "sort_links": _build_sort_links(request, sort_key, sort_dir),
            "dp_rows": [_dp_row_view(r) for r in view.rows],
            "dp_pipeline": [_dp_pipeline_view(i) for i in view.pipeline.all_items_sorted],
            "po_summary": in_transit_summary(db),
        },
    )


@router.get("/reports/demand-planning.csv")
def demand_planning_csv(
    request: Request,
    service_level: str | None = None,
    cover: int | None = None,
    overstocked: int | None = None,
    sort: str | None = None,
    dir: str | None = None,
    db: Session = Depends(get_db),
):
    """CSV export of the current replenishment plan — a single-click PO worksheet.

    Mirrors the columns the buyer sees on screen, with the addition of plain
    numeric versions of money/percent fields so the export drops cleanly into
    Excel/Sheets without quoting tricks. Honors the same query params as
    the HTML view so 'what you see is what you export'.
    """
    sl_dec: Decimal | None = None
    if service_level:
        from app.config import SERVICE_LEVEL_Z_TABLE
        try:
            cand = Decimal(service_level)
            if cand in SERVICE_LEVEL_Z_TABLE:
                sl_dec = cand
        except InvalidOperation:
            sl_dec = None

    expected_receipts: dict[str, int] = {}
    for k, val in request.query_params.items():
        if k.startswith("receipts_") and val.strip():
            try:
                expected_receipts[k[len("receipts_"):]] = int(val)
            except ValueError:
                pass

    view = compute_demand_planning_view(
        db,
        service_level_override=sl_dec,
        cover_days=cover,
        overstocked_days=overstocked,
        expected_receipts=expected_receipts or None,
    )

    sort_key = sort if sort in _DEMAND_SORT_KEYS else None
    sort_dir = "desc" if (dir or "").lower() == "desc" else "asc"
    if sort_key:
        _apply_planner_sort(view.rows, sort_key, sort_dir)

    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "status", "sku", "component_sku", "product",
        "on_hand", "in_transit", "available",
        "daily_velocity_14d", "daily_velocity_60d", "trend_ratio",
        "days_of_supply", "stockout_date",
        "lead_time_days", "reorder_point",
        "suggested_order_qty", "investment_usd",
    ])
    for r in view.rows:
        writer.writerow([
            r.status.value,
            r.sku_code or "",
            r.component_sku,
            r.name or "",
            r.on_hand,
            r.expected_receipts,
            r.available,
            str(r.daily_velocity_14d),
            str(r.daily_velocity),
            str(r.trend_ratio),
            "" if r.days_of_supply is None else str(r.days_of_supply),
            r.stockout_date.isoformat() if r.stockout_date else "",
            r.lead_time_days,
            r.reorder_point,
            r.suggested_order_qty,
            str(r.investment.quantize(Decimal("0.01"))),
        ])

    filename = f"demand-planning-{view.as_of.isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/demand-planning-pipeline.csv")
def demand_planning_pipeline_csv(
    request: Request,
    service_level: str | None = None,
    cover: int | None = None,
    overstocked: int | None = None,
    db: Session = Depends(get_db),
):
    """CSV export of the upcoming purchase pipeline — the next-90-day PO calendar
    bucketed by when each SKU must be ordered. Honors the same query params as
    the demand-planning page so it matches what's shown on screen."""
    sl_dec: Decimal | None = None
    if service_level:
        from app.config import SERVICE_LEVEL_Z_TABLE
        try:
            cand = Decimal(service_level)
            if cand in SERVICE_LEVEL_Z_TABLE:
                sl_dec = cand
        except InvalidOperation:
            sl_dec = None

    expected_receipts: dict[str, int] = {}
    for k, val in request.query_params.items():
        if k.startswith("receipts_") and val.strip():
            try:
                expected_receipts[k[len("receipts_"):]] = int(val)
            except ValueError:
                pass

    view = compute_demand_planning_view(
        db,
        service_level_override=sl_dec,
        cover_days=cover,
        overstocked_days=overstocked,
        expected_receipts=expected_receipts or None,
    )
    p = view.pipeline

    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "bucket", "order_by_date", "status", "sku", "component_sku", "product",
        "on_hand", "reorder_point", "days_until_reorder",
        "suggested_qty", "investment_usd",
    ])
    for bucket, items in [
        ("order_today", p.overdue), ("within_30_days", p.next_30),
        ("within_60_days", p.next_60), ("within_90_days", p.next_90),
    ]:
        for i in items:
            writer.writerow([
                bucket,
                i.order_by_date.isoformat(),
                i.status.value,
                i.sku_code or "",
                i.component_sku,
                i.name or "",
                i.on_hand,
                i.reorder_point,
                i.days_until_reorder,
                i.suggested_qty,
                str(i.investment.quantize(Decimal("0.01"))),
            ])

    filename = f"purchase-pipeline-{view.as_of.isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/reports/demand-planning/sku/{component_sku}/procurement")
def demand_planning_update_procurement(
    component_sku: str,
    request: Request,
    lead_time_days: str = Form(default=""),
    safety_stock_pct: str = Form(default=""),
    moq: str = Form(default=""),
    case_pack: str = Form(default=""),
    unit_cogs: str = Form(default=""),
    is_reorderable: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Update procurement attrs on the matching Sku row. Empty strings clear
    the field (falls back to global default at planner-compute time). 404 if
    the SKU isn't yet in the catalog — must be added via SKU Master first."""
    # The drill-down links by the physical code (Sku.sku), which can map to
    # several variation rows (one per TikTok variation) sharing one physical
    # product. Update them all so the planner's representative row — whichever
    # variation it picks — reflects the edit and the variations stay in sync.
    skus = db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id == component_sku)
            | (Sku.sku == component_sku)
            | (Sku.tiktok_alt_sku == component_sku)
        )
    ).scalars().all()
    if not skus:
        raise HTTPException(
            status_code=404,
            detail=f"SKU {component_sku} is not in the catalog. Add it via the SKU Master upload first.",
        )

    def _int_or_none(s: str) -> int | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            v = int(s)
            return v if v >= 0 else None
        except ValueError:
            return None

    def _dec_or_none(s: str) -> Decimal | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    lead = _int_or_none(lead_time_days)
    moq_v = _int_or_none(moq)
    case_v = _int_or_none(case_pack)
    safety = _dec_or_none(safety_stock_pct)
    safety = safety if (safety is not None and 0 <= safety <= 100) else None
    cogs = _dec_or_none(unit_cogs)
    # Checkbox: present="on" → True; absent → False. NULL is forbidden here
    # because the column is non-null with a True default.
    reorderable = (is_reorderable is not None)

    for sku in skus:
        sku.lead_time_days = lead
        sku.moq = moq_v
        sku.case_pack = case_v
        sku.safety_stock_pct = safety
        if cogs is not None and cogs >= 0:
            sku.unit_cogs = cogs
        sku.is_reorderable = reorderable
    db.commit()

    return RedirectResponse(
        url=f"/reports/demand-planning/sku/{component_sku}?saved=1",
        status_code=303,
    )


@router.get("/reports/demand-planning/sku/{component_sku}")
def demand_planning_sku_detail(
    component_sku: str,
    request: Request,
    safety: str | None = None,
    cover: int | None = None,
    receipts: int = 0,
    saved: int = 0,
    db: Session = Depends(get_db),
):
    """Per-SKU drill-down for the demand planner: weekly velocity, inventory
    history, bundle relationships, and a math walkthrough explaining the
    suggested quantity.
    """
    safety_dec: Decimal | None = None
    if safety:
        try:
            safety_dec = Decimal(safety)
        except InvalidOperation:
            safety_dec = None

    view = compute_sku_detail_view(
        db, component_sku,
        safety_stock_pct=safety_dec,
        cover_days=cover,
        expected_receipts=int(receipts) if receipts else 0,
    )
    if view is None:
        raise HTTPException(status_code=404, detail=f"No data for SKU {component_sku}")

    return templates.TemplateResponse(
        request,
        "reports/demand_planning_sku.html",
        {"view": view},
    )


def _ad_spend_error_redirect(reason: str) -> RedirectResponse:
    qs = urlencode({"error": reason})
    return RedirectResponse(f"/reports/ad-spend?period=month&{qs}", status_code=303)


@router.get("/reports/ad-spend")
def ad_spend_view(
    request: Request,
    scope: str = "month",
    start_date: str | None = None,   # ISO YYYY-MM-DD; scope=range/daily only
    end_date: str | None = None,
    year: int | None = None,         # fiscal_* scopes only
    month: int | None = None,        # fiscal_* scopes only
    db: Session = Depends(get_db),
):
    """Ad Spend & Campaign KPIs, with three scopes:

    - `month` (default) → per-month KPI table (all months) + highlighted totals.
    - `all-time` → the combined figures as a single row.
    - `range` → per-month table scoped to [start_date, end_date] (inclusive),
      with the totals row summarising the range. A month is included if it
      overlaps the window.

    Campaign figures come from the entered GMV Max metrics; spend is GMV-Max only
    (matches TikTok's Ad Cost; Shop Ads stays in the P&L). `daily` lists one row
    per day in [start_date, end_date] with the same campaign-attributed columns
    (no blended ROAS — see compute_ad_spend_daily)."""
    from app.models.gmv_max_daily_metric import GmvMaxDailyMetric

    start = end = None
    range_error: str | None = None
    sd: date | None = None
    ed: date | None = None
    if scope in ("range", "daily"):
        try:
            sd = date.fromisoformat(start_date) if start_date else None
            ed = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            sd = ed = None
            range_error = "Invalid date — use YYYY-MM-DD."
        # Daily defaults to the last 30 days of available data so the first
        # click isn't an empty error; the picker then repaints with these.
        if range_error is None and scope == "daily" and (sd is None or ed is None):
            latest = db.execute(select(func.max(GmvMaxDailyMetric.metric_date))).scalar()
            if latest is not None:
                ed = ed or latest
                sd = sd or (ed - timedelta(days=29))
        if range_error is None and (sd is None or ed is None):
            range_error = "Pick both a start and end date."
        elif range_error is None and sd > ed:
            range_error = "Start date must be on or before end date."
        if range_error is None and scope == "range":
            start = datetime(sd.year, sd.month, sd.day)
            # End is inclusive of the chosen day; the window is [start, end) so
            # bump by one day to cover all of end_date.
            end = datetime(ed.year, ed.month, ed.day) + timedelta(days=1)

    # Fiscal scopes reuse the monthly KPI table, computed over Smashbox fiscal
    # periods (29th–28th) instead of calendar months.
    fiscal_modes = {"fiscal_month": "month", "fiscal_ytd": "ytd", "fiscal_year": "year"}

    daily = None
    monthly = None
    fiscal_year = fiscal_month = None
    fiscal_banner = None
    if scope in fiscal_modes:
        latest = db.execute(select(func.max(GmvMaxDailyMetric.metric_date))).scalar()
        fiscal_year = year or (latest.year if latest else today_local().year)
        fiscal_month = month or (latest.month if latest else today_local().month)
        monthly = compute_ad_spend_fiscal(db, fiscal_year, fiscal_month, fiscal_modes[scope])
        fiscal_banner = fiscal_banner_payload(scope, fiscal_year, fiscal_month)
    elif scope == "daily":
        if range_error is None and sd is not None and ed is not None:
            daily = compute_ad_spend_daily(db, sd, ed)
        # Repaint the pickers with the resolved (possibly defaulted) dates.
        start_date = sd.isoformat() if sd else start_date
        end_date = ed.isoformat() if ed else end_date
    else:
        monthly = compute_ad_spend_monthly(db, start, end)

    # When the GMV-Max (Marketing API) data was last refreshed — completion time
    # of the most-recent import that wrote a daily metric, in shop-local time.
    from app.services.inventory_sync import last_synced_at
    from app.services.reporting_tz import utc_to_shop_local
    _gmv_synced = last_synced_at(db, GmvMaxDailyMetric)
    gmv_synced_at = utc_to_shop_local(_gmv_synced) if _gmv_synced else None

    return templates.TemplateResponse(
        request,
        "reports/ad_spend.html",
        {
            "monthly": monthly,
            "daily": daily,
            "gmv_synced_at": gmv_synced_at,
            "scope": scope,
            "start_date": start_date,
            "end_date": end_date,
            "fiscal_year": fiscal_year,
            "fiscal_month": fiscal_month,
            "fiscal_banner": fiscal_banner,
            "range_error": range_error,
            "all_time": scope == "all-time",
            "error": request.query_params.get("error"),
        },
    )


@router.get("/reports/ad-spend/reimbursements")
def ad_spend_reimbursements_view(request: Request, db: Session = Depends(get_db)):
    """Ad-credit / reimbursement management — the editable credit table, the
    catch-all credit form, the TikTok-reported breakdown, and the explainer
    (moved off the Ad Spend Summary page, which now shows only the KPI tiles)."""
    summary = compute_ad_spend_summary(db)
    months = summary.months
    by_key = {(m.year, m.month): m for m in months}
    row_deltas = []
    for m in months:
        py, pm = (m.year - 1, 12) if m.month == 1 else (m.year, m.month - 1)
        prior = by_key.get((py, pm))
        row_deltas.append(
            compute_delta(m.total, prior.total if prior else None, prior_has_data=prior is not None)
        )
    return templates.TemplateResponse(
        request,
        "reports/ad_spend_reimbursements.html",
        {
            "summary": summary,
            "row_deltas": row_deltas,
            "today": today_local(),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/reports/reconciliation")
def reconciliation_view(year: int | None = None, month: int | None = None):
    """Back-compat: Reconciliation is now the Recon tab of /reports/recon-health.
    Preserve the year/month selection across the redirect."""
    target = "/reports/recon-health?tab=recon"
    if year is not None:
        target += f"&year={year}"
    if month is not None:
        target += f"&month={month}"
    return RedirectResponse(target, status_code=303)
