"""Weekly inventory report email: render the dashboard-styled HTML + text bodies
and a formatted .xlsx attachment from an InventoryReportView, and send it.

Pure formatting + a thin send seam — reuses compute_inventory_report for data
and mailer.send_email for delivery.
"""
from __future__ import annotations

import io
from datetime import datetime

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


def render_inventory_email(view: InventoryReportView) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body). HTML is inline-styled to match the
    dashboard; both bodies state the last-snapshot time up top."""
    date_str = f"{view.as_of:%b %d, %Y}"
    subject = f"Smashbox Weekly Inventory — {date_str}"
    snap = _snapshot_line(view)

    rows_html = []
    for r in view.rows:
        rows_html.append(
            f'<tr><td style="{_TD}">{r.sku_code or "Unmapped"}</td>'
            f'<td style="{_TD}">{(r.name or "")}</td>'
            f'<td style="{_TD_R}">{r.sellable_on_hand}</td>'
            f'<td style="{_TD_R}">{r.sample_on_hand}</td>'
            f'<td style="{_TD_R}">{r.total_on_hand}</td>'
            f'<td style="{_TD_R}">{r.in_transit}</td></tr>'
        )
    totals = (
        f'<tr><td style="{_TOT}" colspan="2">Total · {view.sku_count} SKUs</td>'
        f'<td style="{_TOT_R}">{view.total_sellable}</td>'
        f'<td style="{_TOT_R}">{view.total_sample}</td>'
        f'<td style="{_TOT_R}">{view.total_units}</td>'
        f'<td style="{_TOT_R}">{view.total_in_transit}</td></tr>'
    )
    html = (
        f'<div style="{_CARD}">'
        f'<div style="padding:16px 12px;background:#f8fafc">'
        f'<div style="font-size:16px;font-weight:700;color:#0f172a">Smashbox Weekly Inventory</div>'
        f'<div style="font-size:12px;color:#475569;margin-top:2px">{snap}</div></div>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<thead><tr>'
        f'<th style="{_TH}">SKU</th><th style="{_TH}">Product</th>'
        f'<th style="{_TH_R}">Sellable</th><th style="{_TH_R}">Sample</th>'
        f'<th style="{_TH_R}">Total</th>'
        f'<th style="{_TH_R}">On Order</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}{totals}</tbody></table></div>'
    )

    text_lines = [
        "Smashbox Weekly Inventory", snap, "",
        f"{'SKU':<18}{'Product':<28}{'Sellable':>9}{'Sample':>8}{'Total':>7}{'OnOrder':>8}",
    ]
    for r in view.rows:
        text_lines.append(
            f"{(r.sku_code or 'Unmapped'):<18}{(r.name or '')[:27]:<28}"
            f"{r.sellable_on_hand:>9}{r.sample_on_hand:>8}{r.total_on_hand:>7}{r.in_transit:>8}"
        )
    text_lines.append(
        f"{'TOTAL':<18}{f'{view.sku_count} SKUs':<28}"
        f"{view.total_sellable:>9}{view.total_sample:>8}{view.total_units:>7}{view.total_in_transit:>8}"
    )
    return subject, html, "\n".join(text_lines)
