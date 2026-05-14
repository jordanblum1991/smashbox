"""CSV / Excel export endpoints.

Returns the same data as the HTML report pages, in a downloadable format.
The Excel writer uses xlsxwriter so we can apply number formats and headers.
"""
from datetime import date, datetime
from io import BytesIO

import xlsxwriter
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.sku_profitability import compute_sku_profitability

router = APIRouter(prefix="/export", tags=["exports"])


@router.get("/monthly-pnl.xlsx")
def export_monthly_pnl_xlsx(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    today = date.today()
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
        ("Less: TikTok-Funded Discount", -pnl.platform_discount),
        ("Less: Outlandish-Funded Discount", -pnl.outlandish_discount),
        ("Less: Smashbox-Funded Discount", -pnl.smashbox_discount),
        ("Less: Refunds", -pnl.refunds),
        ("Net Customer Sales", pnl.net_customer_sales),
        ("COGS", -pnl.cogs),
        ("Gross Profit", pnl.gross_profit),
        ("TikTok fees", -pnl.tiktok_fees),
        ("Affiliate commission", -pnl.affiliate_commission),
        ("Shop ads cost", -pnl.shop_ads_cost),
        ("Shipping revenue", pnl.shipping_revenue),
        ("Shipping cost", -pnl.shipping_cost),
        ("Net Profit", pnl.net_profit),
    ]
    for i, (label, value) in enumerate(rows, start=3):
        ws.write(f"A{i}", label)
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


@router.get("/sku-profitability.csv")
def export_sku_csv(
    year: int | None = None,
    month: int | None = None,
    db: Session = Depends(get_db),
):
    today = date.today()
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
