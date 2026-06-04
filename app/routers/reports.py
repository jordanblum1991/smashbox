"""HTML report views.

Each report renders to a Jinja template. Print styles live in static/css/app.css
so any report page can be sent to PDF or paper for brand meetings.
"""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.ad_credit import AdCredit
from app.models.import_batch import _utc_now_naive
from app.models.order import OrderLine
from app.models.sku import Sku
from app.reports.ad_spend import compute_ad_spend_summary
from app.reports.demand_planning import compute_demand_planning_view, compute_sku_detail_view
from app.reports.dashboard_trends import (
    bar_chart,
    build_dashboard_trends,
    compute_delta,
    sparkline_points,
)
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.policy_violations import (
    compute_policy_violations,
    months_with_unacknowledged_violations,
)
from app.reports.reconciliation import (
    daily_sales_reconciliation,
    reconcile_month,
    yearly_sales_reconciliation,
)
from app.reports.sample_inventory import compute_sample_inventory_view
from app.reports.sample_tracking import (
    SamplePeriodKind,
    compute_sample_view,
)
from app.reports.samples_by_creator import compute_samples_by_creator_view
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
        {"view": view, "PeriodKind": PeriodKind, "trends": trends, "charts": charts, "error": error},
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


@router.get("/reports/sample-inventory")
def sample_inventory_view(request: Request, db: Session = Depends(get_db)):
    """Sample pool on-hand inventory, derived from the movement ledger."""
    view = compute_sample_inventory_view(db)
    return templates.TemplateResponse(
        request,
        "reports/sample_inventory.html",
        {"view": view},
    )


@router.get("/reports/samples-by-creator")
def samples_by_creator_view(request: Request, db: Session = Depends(get_db)):
    """Samples sent, grouped by creator."""
    view = compute_samples_by_creator_view(db)
    return templates.TemplateResponse(
        request,
        "reports/samples_by_creator.html",
        {"view": view},
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
    return RedirectResponse("/reports/ad-spend", status_code=303)


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
    return RedirectResponse(f"/reports/ad-spend?{qs}", status_code=303)


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

    return templates.TemplateResponse(
        request,
        "reports/demand_planning.html",
        {
            "view": view,
            "sort_key": sort_key,
            "sort_dir": sort_dir,
            "sort_links": _build_sort_links(request, sort_key, sort_dir),
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
    sku = db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id == component_sku)
            | (Sku.sku == component_sku)
            | (Sku.tiktok_alt_sku == component_sku)
        )
    ).scalar_one_or_none()
    if sku is None:
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

    sku.lead_time_days = _int_or_none(lead_time_days)
    sku.moq = _int_or_none(moq)
    sku.case_pack = _int_or_none(case_pack)
    safety = _dec_or_none(safety_stock_pct)
    sku.safety_stock_pct = safety if (safety is not None and 0 <= safety <= 100) else None
    cogs = _dec_or_none(unit_cogs)
    if cogs is not None and cogs >= 0:
        sku.unit_cogs = cogs
    # Checkbox: present="on" → True; absent → False. NULL is forbidden here
    # because the column is non-null with a True default.
    sku.is_reorderable = (is_reorderable is not None)
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


@router.get("/reports/ad-spend")
def ad_spend_view(request: Request, db: Session = Depends(get_db)):
    """Monthly TikTok ad spend with cash / credit / ad-credit breakdown."""
    summary = compute_ad_spend_summary(db)
    months = summary.months

    # Sparklines for the 3 total tiles — the monthly series the page already has.
    spark_gross = sparkline_points([m.total for m in months])
    spark_credit = sparkline_points([m.manual_credit for m in months])
    spark_net = sparkline_points([m.net_total for m in months])

    # Per-row MoM delta on Gross Ad Spend vs the immediately-preceding CALENDAR
    # month. Neutral polarity — rising ad spend isn't inherently good/bad. The
    # prior must be the actual previous month present in the series; a gap (or
    # the first month) reads "new", never a misleading jump across a gap.
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
        "reports/ad_spend.html",
        {
            "summary": summary,
            "spark_gross": spark_gross,
            "spark_credit": spark_credit,
            "spark_net": spark_net,
            "row_deltas": row_deltas,
            "today": date.today(),
            "error": request.query_params.get("error"),
        },
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
