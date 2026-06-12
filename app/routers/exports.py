"""CSV / Excel export endpoints.

Returns the same data as the HTML report pages, in a downloadable format.
The Excel writer uses xlsxwriter so we can apply number formats and headers.
"""
from datetime import date, datetime, timedelta
from io import BytesIO

import xlsxwriter
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.reports.ad_spend import compute_ad_spend_monthly
from app.reports.inventory_report import compute_inventory_report
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.purchase_statement import compute_purchase_statement
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.sample_tracking import samples_by_sku_shipped
from app.reports.sku_profitability import compute_sku_profitability
from app.services.reporting_tz import today_local

router = APIRouter(prefix="/export", tags=["exports"])


@router.get("/monthly-pnl.xlsx")
def export_monthly_pnl_xlsx(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    today = today_local()
    y, m = year or today.year, month or today.month
    pnl = compute_monthly_pnl(db, y, m)

    import calendar
    month_name = calendar.month_name[m]            # "April"
    month_abbr = calendar.month_abbr[m]            # "Apr"

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(f"{month_abbr} {y}")     # tab: "Apr 2026"
    money = wb.add_format({"num_format": "$#,##0.00"})
    bold = wb.add_format({"bold": True})

    ws.write("A1", f"Smashbox P&L — {month_name} {y}", bold)
    rows = [
        ("Gross Product Sales", pnl.gross_sales),
        ("GMV (TikTok Seller Center)", pnl.gmv),
        ("Less: TikTok-Funded Discount", -pnl.platform_discount),
        ("Less: Outlandish-Funded Discount", -pnl.outlandish_discount),
        ("Less: Smashbox-Funded Discount", -pnl.smashbox_discount),
        ("Smashbox-Funded Discount Reimbursed by Smashbox (contra entry)", pnl.smashbox_discount_offset),
        ("    Smashbox funds this discount directly. It is shown for transparency and offset below so it does not reduce the managed P&L.", None),
        ("Sales (after Discounts)", pnl.managed_sales_pre_refund),
        ("Less: Refunds", -pnl.refunds),
        ("Net Customer Sales", pnl.managed_net_customer_sales),
        ("COGS", -pnl.cogs),
        ("Gross Profit", pnl.managed_gross_profit),
        ("TikTok fees", -pnl.tiktok_fees),
        ("    Referral fee", -pnl.tiktok_referral_fee),
        ("    Transaction fee", -pnl.tiktok_transaction_fee),
        ("    Refund admin fee", -pnl.tiktok_refund_admin_fee),
        ("    Sales tax on referral", -pnl.tiktok_sales_tax_on_referral),
        ("    Smart promo fee (incl. tax)", -pnl.tiktok_smart_promo_fee),
        ("    Campaign fees (resource + service)", -pnl.tiktok_campaign_fees),
        ("    Shop partner commission", -pnl.tiktok_partner_commission),
        ("    Managed service (incl. tax)", -pnl.tiktok_managed_service),
        ("Affiliate Commissions", -pnl.affiliate_commission),
        ("Affiliate Commission (Shop Ads)", -pnl.shop_ads_cost),
        ("TikTok Ads (GMV Max)", -pnl.gmv_max_ad_spend),
        ("Less: GMV Max Reimbursement", pnl.gmv_max_reimbursement),
        ("Less: Ad Credits", pnl.ad_credit_offset),
        ("Shipping revenue", pnl.shipping_revenue),
        ("Shipping (to Customers)", -pnl.shipping_cost),
        ("Shipping (to Creators)", -pnl.sample_shipping_cost),
        ("TikTok Reimbursements & Adjustments", pnl.tiktok_adjustments_net),
        ("Net Profit", pnl.managed_net_profit),
    ]
    for i, (label, value) in enumerate(rows, start=3):
        ws.write(f"A{i}", label)
        # value=None marks a note row (label only, no number).
        if value is not None:
            ws.write_number(f"B{i}", float(value), money)
    ws.set_column(0, 0, 36)
    ws.set_column(1, 1, 18)
    wb.close()

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="smashbox_pnl_{y}-{m:02d}.xlsx"'},
    )


@router.get("/ad-spend.xlsx")
def export_ad_spend_xlsx(
    scope: str = "month",
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    """Download the Ad Spend & Campaign KPIs table (per-month rows + a totals
    row), honoring the page's scope (month / all-time / range). Spend is GMV-Max
    only, matching the page."""
    start = end = None
    if scope == "range":
        try:
            sd = date.fromisoformat(start_date) if start_date else None
            ed = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            sd = ed = None
        if sd and ed and sd <= ed:
            start = datetime(sd.year, sd.month, sd.day)
            end = datetime(ed.year, ed.month, ed.day) + timedelta(days=1)   # inclusive end

    monthly = compute_ad_spend_monthly(db, start, end)
    ct = monthly.campaign_total

    import calendar
    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Ad Spend")
    bold = wb.add_format({"bold": True})
    hdr = wb.add_format({"bold": True, "bottom": 1})
    money = wb.add_format({"num_format": "$#,##0.00"})
    num2 = wb.add_format({"num_format": "0.00"})
    roas_fmt = wb.add_format({"num_format": '0.00"x"'})
    t = wb.add_format({"bold": True, "top": 2})
    t_money = wb.add_format({"bold": True, "top": 2, "num_format": "$#,##0.00"})
    t_num2 = wb.add_format({"bold": True, "top": 2, "num_format": "0.00"})
    t_roas = wb.add_format({"bold": True, "top": 2, "num_format": '0.00"x"'})

    ws.write("A1", "Smashbox — Ad Spend & Campaign KPIs", bold)
    headers = ["Month", "SKU Orders", "Cost per Order", "Gross Revenue",
               "ROI", "Total Gross Spend", "ROAS"]
    for c, h in enumerate(headers):
        ws.write(2, c, h, hdr)

    r = 3
    for row in monthly.rows:
        ws.write(r, 0, f"{calendar.month_abbr[row.month]}-{row.year}")
        if row.sku_orders is not None:
            ws.write_number(r, 1, row.sku_orders)
        else:
            ws.write(r, 1, "—")
        if row.cost_per_order is not None:
            ws.write_number(r, 2, float(row.cost_per_order), money)
        else:
            ws.write(r, 2, "—")
        if row.gross_revenue is not None:
            ws.write_number(r, 3, float(row.gross_revenue), money)
        else:
            ws.write(r, 3, "—")
        if row.roi is not None:
            ws.write_number(r, 4, float(row.roi), num2)
        else:
            ws.write(r, 4, "—")
        ws.write_number(r, 5, float(row.gross_spend), money)
        ws.write_number(r, 6, float(row.roas), roas_fmt)
        r += 1

    # Totals row — campaign aggregate + GMV-Max spend total + blended ROAS.
    ws.write(r, 0, "Total", t)
    if ct and ct.has_data:
        ws.write_number(r, 1, ct.sku_orders, t)
        ws.write_number(r, 2, float(ct.cost_per_order), t_money) if ct.sku_orders > 0 else ws.write(r, 2, "—", t)
        ws.write_number(r, 3, float(ct.gross_revenue), t_money)
        ws.write_number(r, 4, float(ct.roi), t_num2) if ct.ad_cost > 0 else ws.write(r, 4, "—", t)
    else:
        for c in (1, 2, 3, 4):
            ws.write(r, c, "—", t)
    ws.write_number(r, 5, float(monthly.total_gross), t_money)
    ws.write_number(r, 6, float(monthly.total_roas), t_roas) if monthly.total_gross > 0 else ws.write(r, 6, "—", t)

    ws.set_column(0, 0, 12)
    ws.set_column(1, 6, 16)
    wb.close()
    buf.seek(0)

    fname = "smashbox_ad_spend"
    if scope == "all-time":
        fname += "_all-time"
    elif scope == "range" and start and end:
        fname += f"_{start_date}_to_{end_date}"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}.xlsx"'},
    )


@router.get("/product-invoice-statement.xlsx")
def export_purchase_statement_xlsx(
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    """Download the Smashbox Product Invoice statement (opening balance, the
    debit/credit/payment ledger with running balance, totals, closing balance),
    honoring an optional [start_date, end_date] window."""
    start = end = None
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        start = end = None
    if start and end and start > end:
        start = end = None

    stmt = compute_purchase_statement(db, start, end)

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Statement")
    bold = wb.add_format({"bold": True})
    hdr = wb.add_format({"bold": True, "bottom": 1})
    money = wb.add_format({"num_format": "$#,##0.00"})
    t = wb.add_format({"bold": True, "top": 2})
    t_money = wb.add_format({"bold": True, "top": 2, "num_format": "$#,##0.00"})

    ws.write("A1", "Smashbox — Product Invoice Statement", bold)
    period = (f"{start.isoformat()} to {end.isoformat()}" if (start and end) else "All-time")
    ws.write("A2", period)

    headers = ["Date", "Description", "Debit", "Credit", "Payment", "Balance"]
    for c, h in enumerate(headers):
        ws.write(3, c, h, hdr)

    r = 4
    ws.write(r, 1, "Opening balance")
    ws.write_number(r, 5, float(stmt.opening_balance), money)
    r += 1
    for row in stmt.rows:
        ws.write(r, 0, row.date.isoformat())
        ws.write(r, 1, row.description)
        if row.debit:
            ws.write_number(r, 2, float(row.debit), money)
        if row.credit:
            ws.write_number(r, 3, float(row.credit), money)
        if row.payment:
            ws.write_number(r, 4, float(row.payment), money)
        ws.write_number(r, 5, float(row.balance), money)
        r += 1

    ws.write(r, 1, "Totals / Closing balance", t)
    ws.write(r, 0, "", t)
    ws.write_number(r, 2, float(stmt.total_debits), t_money)
    ws.write_number(r, 3, float(stmt.total_credits), t_money)
    ws.write_number(r, 4, float(stmt.total_payments), t_money)
    ws.write_number(r, 5, float(stmt.closing_balance), t_money)

    ws.set_column(0, 0, 12)
    ws.set_column(1, 1, 34)
    ws.set_column(2, 5, 14)
    wb.close()
    buf.seek(0)

    fname = "smashbox_product_invoice_statement"
    if start and end:
        fname += f"_{start.isoformat()}_to_{end.isoformat()}"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}.xlsx"'},
    )


@router.get("/sku-profitability.csv")
def export_sku_csv(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    today = today_local()
    y, m = year or today.year, month or today.month
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    rows = compute_sku_profitability(db, start, end)

    def gen():
        yield "tiktok_sku_id,sku_code,name,is_bundle,units_sold,gross_sales,cogs,gross_profit,gross_margin\n"
        for r in rows:
            name = (r.name or "").replace(",", " ")
            yield (
                f"{r.tiktok_sku_id},{r.sku_code or ''},{name},{r.is_bundle},{r.units_sold},"
                f"{r.gross_sales},{r.cogs},{r.gross_profit},{r.gross_margin}\n"
            )

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="smashbox_sku_{y}-{m:02d}.csv"'},
    )


@router.get("/inventory.csv")
def export_inventory_csv(db: Session = Depends(get_db)):
    """Complete inventory (all SKUs, sellable + sample on-hand) as CSV."""
    view = compute_inventory_report(db)

    def gen():
        yield ("sku_code,name,is_bundle,canonical_sku,sellable_on_hand,sample_on_hand,"
               "total_on_hand,unit_cogs,sellable_value,sample_value,total_value\n")
        for r in view.rows:
            name = (r.name or "").replace(",", " ")
            sku = (r.sku_code or "Unmapped").replace(",", " ")
            yield (
                f"{sku},{name},{r.is_bundle},{r.canonical_sku},"
                f"{r.sellable_on_hand},{r.sample_on_hand},{r.total_on_hand},"
                f"{r.unit_cogs:.4f},{r.sellable_value:.2f},{r.sample_value:.2f},{r.total_value:.2f}\n"
            )

    stamp = view.last_synced_at.strftime("%Y%m%d") if view.last_synced_at else "current"
    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="smashbox_inventory_{stamp}.csv"'},
    )


@router.get("/samples-by-sku.csv")
def export_samples_by_sku_csv(
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    db: Session = Depends(get_db),
):
    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    start, end = window_for(view)
    rows = samples_by_sku_shipped(db, start, end)

    def gen():
        yield "sku_code,name,tiktok_sku_id,samples_sent,sample_orders_shipped,units_sold,sold_per_sample\n"
        for r in rows:
            name = (r.name or "").replace(",", " ")
            sku = (r.sku_code or "Unmapped").replace(",", " ")
            yield (
                f"{sku},{name},{r.tiktok_sku_id or ''},"
                f"{r.samples_sent},{r.sample_orders_shipped},{r.units_sold},"
                f"{r.sold_per_sample:.2f}\n"
            )

    filename = f"smashbox_samples_{view.year}"
    if view.month:
        filename += f"-{view.month:02d}"
    filename += ".csv"

    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/pnl.xlsx")
def export_pnl_xlsx(
    period: PeriodKind = PeriodKind.MONTH,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    """Sectioned P&L workbook for any period (month / YTD / year / range / custom).

    One worksheet: title block, period summary KPIs, then the full statement
    with section headers, indented sub-items, bolded subtotals, monthly
    columns + total. Accounting-style negatives via Excel number-format —
    negative values render as `($1,234.56)` in red automatically.

    For CUSTOM (arbitrary date range) the breakdown collapses to a single
    column labelled with the date range — same code path as MONTH.
    """
    sd_obj = date.fromisoformat(start_date) if start_date and period == PeriodKind.CUSTOM else None
    ed_obj = date.fromisoformat(end_date) if end_date and period == PeriodKind.CUSTOM else None
    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
        start_date=sd_obj, end_date=ed_obj,
    )
    pnl = view.total

    # Column structure: per-month columns when we have a breakdown, plus a
    # rollup column on the right. Single-month → one column, no rollup.
    if view.monthly_breakdown:
        months = view.monthly_breakdown
        include_total_col = True
        total_label = "Year" if view.period_kind == PeriodKind.YEAR else "YTD"
    else:
        months = [pnl]
        include_total_col = False
        total_label = None

    # Determine if any month has an ad credit; controls whether we render the
    # "Less: Ad Credits" line at all.
    any_credit = pnl.ad_credit_offset > 0 or any(m.ad_credit_offset > 0 for m in months)

    # Same any-month check for GMV Max reimbursements. Independent flag from
    # any_credit — both lines render iff their respective flag is true.
    any_gmv_reimb = (
        pnl.gmv_max_reimbursement > 0
        or any(m.gmv_max_reimbursement > 0 for m in months)
    )

    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("P&L")

    # ---- Formats ---- ------------------------------------------------------
    money_pattern = '_($* #,##0.00_);[Red]_($* (#,##0.00);_($* "-"??_);_(@_)'
    pct_pattern = '0.0%'

    f_title = wb.add_format({"bold": True, "font_size": 16, "font_color": "#0f172a"})
    f_subtitle = wb.add_format({"bold": True, "font_color": "#0369a1", "font_size": 11})

    f_kpi_label = wb.add_format({"bold": True, "font_color": "#475569", "font_size": 9})
    f_kpi_money = wb.add_format({"bold": True, "font_size": 11, "num_format": money_pattern})
    f_kpi_pct = wb.add_format({"bold": True, "font_size": 11, "num_format": pct_pattern})

    f_section = wb.add_format({
        "bold": True, "font_color": "#475569", "font_size": 9,
        "bg_color": "#f1f5f9", "top": 2, "border_color": "#cbd5e1",
        "italic": True,
    })

    f_line = wb.add_format({"font_color": "#334155"})
    f_line_indent = wb.add_format({"font_color": "#475569", "indent": 1})
    f_line_indent2 = wb.add_format({"font_color": "#64748b", "indent": 2, "italic": True})

    f_money = wb.add_format({"num_format": money_pattern, "font_color": "#334155"})
    f_money_indent = wb.add_format({"num_format": money_pattern, "font_color": "#475569"})
    f_money_indent2 = wb.add_format({"num_format": money_pattern, "font_color": "#64748b", "italic": True})

    f_subtotal_label = wb.add_format({
        "bold": True, "bg_color": "#e2e8f0", "top": 1, "border_color": "#94a3b8",
        "font_color": "#0f172a",
    })
    f_subtotal_money = wb.add_format({
        "bold": True, "bg_color": "#e2e8f0", "top": 1, "border_color": "#94a3b8",
        "num_format": money_pattern, "font_color": "#0f172a",
    })
    f_netprofit_label = wb.add_format({
        "bold": True, "font_size": 12, "bg_color": "#cbd5e1", "top": 2,
        "border_color": "#475569", "font_color": "#0f172a",
    })
    f_netprofit_money = wb.add_format({
        "bold": True, "font_size": 12, "bg_color": "#cbd5e1", "top": 2,
        "border_color": "#475569", "num_format": money_pattern, "font_color": "#0f172a",
    })

    f_header = wb.add_format({
        "bold": True, "font_color": "#475569", "bg_color": "#f8fafc",
        "bottom": 2, "border_color": "#94a3b8", "align": "center",
    })
    f_header_total = wb.add_format({
        "bold": True, "font_color": "#0f172a", "bg_color": "#e2e8f0",
        "bottom": 2, "left": 1, "border_color": "#94a3b8", "align": "center",
    })

    # ---- Title + period ---- ----------------------------------------------
    ws.write("A1", "Profit & Loss Statement", f_title)
    ws.write("A2", view.title_suffix, f_subtitle)

    # ---- Summary KPI block ---- --------------------------------------------
    kpi_row = 4
    ws.write(kpi_row, 0, "Summary", wb.add_format({"bold": True, "font_size": 10}))
    kpis = [
        ("Gross Sales", float(pnl.gross_sales), f_kpi_money),
        ("GMV (Seller Center)", float(pnl.gmv), f_kpi_money),
        ("Net Customer Sales", float(pnl.managed_net_customer_sales), f_kpi_money),
        ("Gross Profit", float(pnl.managed_gross_profit), f_kpi_money),
        ("Total Ad Spend (net)", float(pnl.net_ad_spend), f_kpi_money),
        ("Net Profit", float(pnl.managed_net_profit), f_kpi_money),
        ("Gross Margin", float(pnl.managed_gross_margin), f_kpi_pct),
        ("Net Margin", float(pnl.managed_net_margin), f_kpi_pct),
    ]
    for i, (label, value, fmt) in enumerate(kpis):
        ws.write(kpi_row + 1 + i, 0, label, f_kpi_label)
        ws.write_number(kpi_row + 1 + i, 1, value, fmt)

    # ---- Statement table ---- ----------------------------------------------
    table_row = kpi_row + 2 + len(kpis)

    # Header row
    ws.write(table_row, 0, "Line", f_header)
    for i, m in enumerate(months):
        if view.monthly_breakdown:
            col_label = m.month.strftime("%b %y")
        else:
            col_label = view.title_suffix
        ws.write(table_row, 1 + i, col_label, f_header)
    if include_total_col:
        ws.write(table_row, 1 + len(months), total_label, f_header_total)

    def _write_money_row(row, label, attr, sign, label_fmt, money_fmt, total_money_fmt=None):
        ws.write(row, 0, label, label_fmt)
        for i, m in enumerate(months):
            ws.write_number(row, 1 + i, float(getattr(m, attr)) * sign, money_fmt)
        if include_total_col:
            ws.write_number(
                row, 1 + len(months),
                float(getattr(view.total, attr)) * sign,
                total_money_fmt or money_fmt,
            )

    def _write_section_header(row, label):
        ws.merge_range(
            row, 0, row, 1 + len(months) + (1 if include_total_col else 0) - 1,
            label, f_section,
        )

    row = table_row + 1

    # ---- Render the statement ---- -----------------------------------------
    _write_section_header(row, "REVENUE"); row += 1
    _write_money_row(row, "Gross Product Sales", "gross_sales", 1, f_line, f_money); row += 1

    _write_section_header(row, "DISCOUNTS & REFUNDS"); row += 1
    _write_money_row(row, "TikTok-Funded Discount", "platform_discount", -1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Outlandish-Funded Discount", "outlandish_discount", -1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Smashbox-Funded Discount", "smashbox_discount", -1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Smashbox-Funded Discount Reimbursed by Smashbox (contra entry)", "smashbox_discount_offset", 1, f_line_indent, f_money_indent); row += 1
    # Inline note explaining the contra pair, directly under the offset row.
    ws.write(
        row, 0,
        "    Smashbox funds this discount directly. It is shown for transparency "
        "and offset below so it does not reduce the managed P&L.",
        f_line_indent2,
    ); row += 1
    _write_money_row(row, "SALES (AFTER DISCOUNTS)", "managed_sales_pre_refund", 1, f_subtotal_label, f_subtotal_money); row += 1
    _write_money_row(row, "Refunds", "refunds", -1, f_line_indent, f_money_indent); row += 1

    _write_money_row(row, "NET CUSTOMER SALES", "managed_net_customer_sales", 1, f_subtotal_label, f_subtotal_money); row += 1

    _write_section_header(row, "COST OF GOODS SOLD"); row += 1
    _write_money_row(row, "COGS", "cogs", -1, f_line_indent, f_money_indent); row += 1

    _write_money_row(row, "GROSS PROFIT", "managed_gross_profit", 1, f_subtotal_label, f_subtotal_money); row += 1

    _write_section_header(row, "TIKTOK FEES"); row += 1
    _write_money_row(row, "TikTok fees (total)", "tiktok_fees", -1, f_line_indent, f_money_indent); row += 1
    fee_details = [
        ("Referral fee", "tiktok_referral_fee"),
        ("Transaction fee", "tiktok_transaction_fee"),
        ("Refund admin fee", "tiktok_refund_admin_fee"),
        ("Sales tax on referral", "tiktok_sales_tax_on_referral"),
        ("Smart promo fee (incl. tax)", "tiktok_smart_promo_fee"),
        ("Campaign fees (resource + service)", "tiktok_campaign_fees"),
        ("Shop partner commission", "tiktok_partner_commission"),
        ("Managed service (incl. tax)", "tiktok_managed_service"),
    ]
    for label, attr in fee_details:
        _write_money_row(row, label, attr, -1, f_line_indent2, f_money_indent2); row += 1

    _write_section_header(row, "AFFILIATE / COMMISSION COSTS"); row += 1
    _write_money_row(row, "Affiliate Commissions", "affiliate_commission", -1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Affiliate Commission (Shop Ads)", "shop_ads_cost", -1, f_line_indent, f_money_indent); row += 1

    _write_section_header(row, "ADVERTISING"); row += 1
    _write_money_row(row, "TikTok Ads (GMV Max)", "gmv_max_ad_spend", -1, f_line_indent, f_money_indent); row += 1
    if any_gmv_reimb:
        _write_money_row(row, "Less: GMV Max Reimbursement", "gmv_max_reimbursement", 1, f_line_indent2, f_money_indent2); row += 1
    if any_credit:
        _write_money_row(row, "Less: Ad Credits", "ad_credit_offset", 1, f_line_indent2, f_money_indent2); row += 1

    _write_section_header(row, "SHIPPING / FULFILLMENT"); row += 1
    _write_money_row(row, "Shipping revenue", "shipping_revenue", 1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Shipping (to Customers)", "shipping_cost", -1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "Shipping (to Creators)", "sample_shipping_cost", -1, f_line_indent, f_money_indent); row += 1

    _write_money_row(row, "TOTAL OPERATING EXPENSES", "total_operating_expenses", -1, f_subtotal_label, f_subtotal_money); row += 1
    _write_money_row(row, "TikTok Reimbursements & Adjustments", "tiktok_adjustments_net", 1, f_line_indent, f_money_indent); row += 1
    _write_money_row(row, "NET PROFIT", "managed_net_profit", 1, f_netprofit_label, f_netprofit_money); row += 1

    # ---- Layout ---- -------------------------------------------------------
    ws.set_column(0, 0, 40)
    n_data_cols = len(months) + (1 if include_total_col else 0)
    ws.set_column(1, n_data_cols, 14)
    # Freeze the header rows + the first column so the labels and dates stay
    # visible while scrolling.
    ws.freeze_panes(table_row + 1, 1)

    wb.close()
    buf.seek(0)

    safe_suffix = (
        view.title_suffix.replace(" ", "_").replace("/", "-").replace("–", "to").replace("—", "to")
    )
    filename = f"smashbox_pnl_{safe_suffix}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
