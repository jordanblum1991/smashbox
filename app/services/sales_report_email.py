# app/services/sales_report_email.py
"""Email the Sales velocity report: an inline-styled HTML summary (KPIs + the
velocity table) and the velocity CSV — both built from the same view.buckets, so
the HTML and the attachment always match. Thin send seam over mailer.send_email."""
from __future__ import annotations

import csv
import io
from html import escape

from sqlalchemy.orm import Session

from app.services import mailer
from app.services.report_email_common import (
    CARD, HEADER, H_TITLE, H_SUB, TH, TH_R, TD, TD_R, TOT, TOT_R,
)

_HEADERS = ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"]


def _csv_rows(view):
    for b in view.buckets:
        yield [b.label, b.start.isoformat(), f"{b.revenue:.2f}", b.units, b.orders,
               f"{b.aov:.2f}", "yes" if b.in_progress else ""]


def build_sales_csv(view) -> bytes:
    """The sales velocity CSV — identical columns to /reports/sales.csv."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADERS)
    for row in _csv_rows(view):
        w.writerow(row)
    return buf.getvalue().encode()


def render_sales_email(view, *, window_label: str):
    """(subject, html, text). HTML KPIs + velocity table from the same view."""
    subject = f"Smashbox Sales Report — {window_label}"
    kpis = (
        f'<div style="{H_SUB}">Revenue {view.total_revenue:,.2f} · '
        f'Units {view.total_units} · Orders {view.total_orders} · '
        f'AOV {view.avg_aov:,.2f} · Avg/day {view.avg_daily_revenue:,.2f}</div>'
    )
    rows = []
    for b in view.buckets:
        rows.append(
            f'<tr><td style="{TD}">{escape(b.label)}</td>'
            f'<td style="{TD}">{b.start.isoformat()}</td>'
            f'<td style="{TD_R}">{b.revenue:,.2f}</td>'
            f'<td style="{TD_R}">{b.units}</td>'
            f'<td style="{TD_R}">{b.orders}</td>'
            f'<td style="{TD_R}">{b.aov:,.2f}</td>'
            f'<td style="{TD}">{"in progress" if b.in_progress else ""}</td></tr>'
        )
    total = (
        f'<tr><td style="{TOT}" colspan="2">Total</td>'
        f'<td style="{TOT_R}">{view.total_revenue:,.2f}</td>'
        f'<td style="{TOT_R}">{view.total_units}</td>'
        f'<td style="{TOT_R}">{view.total_orders}</td>'
        f'<td style="{TOT_R}">{view.avg_aov:,.2f}</td><td style="{TOT}"></td></tr>'
    )
    html = (
        f'<div style="{CARD}"><div style="{HEADER}">'
        f'<div style="{H_TITLE}">Smashbox Sales Report</div>'
        f'<div style="{H_SUB}">{escape(window_label)}</div>{kpis}</div>'
        f'<table style="width:100%;border-collapse:collapse"><thead><tr>'
        f'<th style="{TH}">Period</th><th style="{TH}">Start</th>'
        f'<th style="{TH_R}">Revenue</th><th style="{TH_R}">Units</th>'
        f'<th style="{TH_R}">Orders</th><th style="{TH_R}">AOV</th>'
        f'<th style="{TH}">Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}{total}</tbody></table></div>'
    )
    text_lines = [f"Smashbox Sales Report — {window_label}",
                  f"Revenue {view.total_revenue:,.2f} · Units {view.total_units} · "
                  f"Orders {view.total_orders} · AOV {view.avg_aov:,.2f}", ""]
    for b in view.buckets:
        text_lines.append(f"{b.label:<14}{b.revenue:>12,.2f}{b.units:>7}{b.orders:>7}")
    return subject, html, "\n".join(text_lines)


def send_sales_report(db: Session, *, recipients: list[str], granularity: str,
                      start_date, end_date, year, month) -> None:
    """Compute the sales view for the given scope, render, attach the CSV, send."""
    if not recipients:
        raise ValueError("no recipients configured for the sales report")
    # Local import avoids a router import cycle.
    from app.routers.reports import _sales_view_data
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    view = ctx["view"]
    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    subject, html, text = render_sales_email(view, window_label=window_label)
    csv_bytes = build_sales_csv(view)
    fname = f"smashbox_sales_{view.window_start:%Y%m%d}_{view.window_end:%Y%m%d}.csv"
    mailer.send_email(subject, text, to=recipients, html=html,
                      attachments=[(fname, csv_bytes, "csv")])
