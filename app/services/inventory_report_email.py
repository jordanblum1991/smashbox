"""Weekly inventory report email: render the dashboard-styled HTML + text bodies
and a formatted .xlsx attachment from an InventoryReportView, and send it.

Pure formatting + a thin send seam — reuses compute_inventory_report for data
and mailer.send_email for delivery.
"""
from __future__ import annotations

import io
from html import escape

import xlsxwriter
from sqlalchemy.orm import Session

from app.reports.inventory_report import InventoryReportView, compute_inventory_report
from app.services import mailer

# Inline styles approximating the dashboard (email clients strip Tailwind/CSS).
_CARD = ("border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;"
         "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
         "max-width:760px")
_TH = ("padding:8px 12px;text-align:left;font-size:10px;font-weight:600;"
       "text-transform:uppercase;letter-spacing:.05em;color:#64748b;"
       "border-bottom:1px solid #e2e8f0")
_TH_R = _TH + ";text-align:right"
_TD = "padding:8px 12px;font-size:13px;color:#0f172a;border-bottom:1px solid #f1f5f9"
_TD_R = _TD + ";text-align:right;font-variant-numeric:tabular-nums"
_TOT = "padding:8px 12px;font-size:13px;font-weight:700;color:#0f172a;border-top:2px solid #e2e8f0"
_TOT_R = _TOT + ";text-align:right;font-variant-numeric:tabular-nums"


def _snapshot_line(view: InventoryReportView) -> str:
    if view.last_synced_at is None:
        return "No snapshot yet — inventory has not been synced."
    return f"Inventory as of {view.last_synced_at:%Y-%m-%d %H:%M} (shop local)"


def _email_sorted_rows(view: InventoryReportView) -> list:
    """Rows ordered for the email: items with stock or inbound first, LARGEST
    total units first (sellable + sample on-hand + on-order sellable + on-order
    sample); all-zero rows sink to the bottom. sku_code breaks ties for a stable
    order."""
    return sorted(
        view.rows,
        key=lambda r: (-(r.total_on_hand + r.in_transit + r.sample_in_transit),
                       r.sku_code or "~"),
    )


def render_inventory_email(view: InventoryReportView) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body). HTML is inline-styled to match the
    dashboard; both bodies state the last-snapshot time up top."""
    date_str = f"{view.as_of:%b %d, %Y}"
    subject = f"Smashbox Weekly Inventory Snapshot — {date_str}"
    snap = _snapshot_line(view)

    snap_html = escape(snap)
    rows_html = []
    for r in _email_sorted_rows(view):
        rows_html.append(
            f'<tr><td style="{_TD}">{escape(r.sku_code or "Unmapped")}</td>'
            f'<td style="{_TD}">{escape(r.name or "")}</td>'
            f'<td style="{_TD_R}">{r.sellable_on_hand}</td>'
            f'<td style="{_TD_R}">{r.sample_on_hand}</td>'
            f'<td style="{_TD_R}">{r.total_on_hand}</td>'
            f'<td style="{_TD_R}">{r.in_transit}</td>'
            f'<td style="{_TD_R}">{r.sample_in_transit}</td></tr>'
        )
    totals = (
        f'<tr><td style="{_TOT}" colspan="2">Total · {view.sku_count} SKUs</td>'
        f'<td style="{_TOT_R}">{view.total_sellable}</td>'
        f'<td style="{_TOT_R}">{view.total_sample}</td>'
        f'<td style="{_TOT_R}">{view.total_units}</td>'
        f'<td style="{_TOT_R}">{view.total_in_transit}</td>'
        f'<td style="{_TOT_R}">{view.total_sample_in_transit}</td></tr>'
    )
    html = (
        f'<div style="{_CARD}">'
        f'<div style="padding:16px 12px;background:#f8fafc">'
        f'<div style="font-size:16px;font-weight:700;color:#0f172a">Smashbox Weekly Inventory Snapshot</div>'
        f'<div style="font-size:12px;color:#475569;margin-top:2px">{snap_html}</div></div>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<thead><tr>'
        f'<th style="{_TH}">SKU</th><th style="{_TH}">Product</th>'
        f'<th style="{_TH_R}">Sellable</th><th style="{_TH_R}">Sample</th>'
        f'<th style="{_TH_R}">Total</th>'
        f'<th style="{_TH_R}">On order (sellable)</th>'
        f'<th style="{_TH_R}">On order (sample)</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}{totals}</tbody></table></div>'
    )

    text_lines = [
        "Smashbox Weekly Inventory Snapshot", snap, "",
        f"{'SKU':<18}{'Product':<26}{'Sellable':>9}{'Sample':>8}{'Total':>7}"
        f"{'On order (sellable)':>21}{'On order (sample)':>20}",
    ]
    for r in _email_sorted_rows(view):
        text_lines.append(
            f"{(r.sku_code or 'Unmapped'):<18}{(r.name or '')[:25]:<26}"
            f"{r.sellable_on_hand:>9}{r.sample_on_hand:>8}{r.total_on_hand:>7}"
            f"{r.in_transit:>21}{r.sample_in_transit:>20}"
        )
    text_lines.append(
        f"{'TOTAL':<18}{f'{view.sku_count} SKUs':<26}"
        f"{view.total_sellable:>9}{view.total_sample:>8}{view.total_units:>7}"
        f"{view.total_in_transit:>21}{view.total_sample_in_transit:>20}"
    )
    return subject, html, "\n".join(text_lines)


def build_inventory_xlsx(view: InventoryReportView) -> bytes:
    """A formatted inventory workbook: frozen bold header, autofilter, column
    widths, integer number formats, and a bold totals row. A caption cell
    carries the snapshot age so it travels with the file. Units only — COGS and
    value columns are intentionally excluded."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Inventory")

    title = wb.add_format({"bold": True, "font_size": 14})
    caption = wb.add_format({"font_color": "#475569"})
    hdr = wb.add_format({"bold": True, "bg_color": "#f1f5f9", "bottom": 1,
                         "align": "left"})
    hdr_r = wb.add_format({"bold": True, "bg_color": "#f1f5f9", "bottom": 1,
                           "align": "right"})
    num = wb.add_format({"num_format": "#,##0"})
    tot = wb.add_format({"bold": True, "top": 2})
    tot_n = wb.add_format({"bold": True, "top": 2, "num_format": "#,##0"})

    ws.write("A1", "Smashbox — Weekly Inventory Snapshot", title)
    # _snapshot_line returns "Inventory as of …" when a snapshot exists; give a
    # parallel "Inventory as of: …" caption when none, so the file always carries
    # the snapshot age (and a reader always sees the data's freshness).
    ws.write("A2", _snapshot_line(view) if view.last_synced_at
             else "Inventory as of: no snapshot yet", caption)

    headers = ["SKU", "Product", "Sellable", "Sample", "Total On Hand",
               "On order (sellable)", "On order (sample)"]
    hrow = 3
    for c, h in enumerate(headers):
        ws.write(hrow, c, h, hdr_r if c >= 2 else hdr)

    r = hrow + 1
    for row in _email_sorted_rows(view):
        ws.write(r, 0, row.sku_code or "Unmapped")
        ws.write(r, 1, row.name or "")
        ws.write_number(r, 2, row.sellable_on_hand, num)
        ws.write_number(r, 3, row.sample_on_hand, num)
        ws.write_number(r, 4, row.total_on_hand, num)
        ws.write_number(r, 5, row.in_transit, num)
        ws.write_number(r, 6, row.sample_in_transit, num)
        r += 1

    ws.write(r, 0, "TOTAL", tot)
    ws.write(r, 1, f"{view.sku_count} SKUs", tot)
    ws.write_number(r, 2, view.total_sellable, tot_n)
    ws.write_number(r, 3, view.total_sample, tot_n)
    ws.write_number(r, 4, view.total_units, tot_n)
    ws.write_number(r, 5, view.total_in_transit, tot_n)
    ws.write_number(r, 6, view.total_sample_in_transit, tot_n)

    ws.freeze_panes(hrow + 1, 0)
    ws.autofilter(hrow, 0, r - 1, len(headers) - 1)
    ws.set_column(0, 0, 18)
    ws.set_column(1, 1, 34)
    ws.set_column(2, 4, 13)
    ws.set_column(5, 6, 18)
    wb.close()
    buf.seek(0)
    return buf.getvalue()


def send_inventory_report(db: Session, *, recipients: list[str]) -> None:
    """Compute the current inventory report, render it, attach the formatted
    workbook, and email it to `recipients`. Raises ValueError on empty
    recipients; propagates any send error to the caller."""
    if not recipients:
        raise ValueError("no recipients configured for the inventory report")
    view = compute_inventory_report(db)
    subject, html, text = render_inventory_email(view)
    xlsx = build_inventory_xlsx(view)
    stamp = view.last_synced_at.strftime("%Y%m%d") if view.last_synced_at else "current"
    mailer.send_email(
        subject, text, to=recipients, html=html,
        attachments=[(f"smashbox_inventory_{stamp}.xlsx", xlsx,
                      "vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
