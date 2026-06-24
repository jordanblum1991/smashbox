# app/services/sample_report_email.py
"""Email the Sample report: an inline-styled HTML by-SKU summary (totals + the
shipped-samples table) and the samples-by-SKU CSV — both built from the same
samples_by_sku_shipped rows, so the HTML and the attachment always match. Thin
send seam over mailer.send_email."""
from __future__ import annotations

from html import escape

from sqlalchemy.orm import Session

from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.sample_tracking import samples_by_sku_shipped
from app.services import mailer
from app.services.report_email_common import (
    CARD, HEADER, H_TITLE, H_SUB, TH, TH_R, TD, TD_R, TOT, TOT_R,
)

_HEADERS = ["sku_code", "name", "tiktok_sku_id", "samples_sent",
            "sample_orders_shipped", "units_sold", "sold_per_sample"]


def _csv_rows(rows):
    """Mirror the legacy /samples-by-sku.csv body byte-for-byte: name/sku escape
    commas to spaces (no quoting), sku falls back to 'Unmapped'."""
    for r in rows:
        name = (r.name or "").replace(",", " ")
        sku = (r.sku_code or "Unmapped").replace(",", " ")
        yield (
            f"{sku},{name},{r.tiktok_sku_id or ''},"
            f"{r.samples_sent},{r.sample_orders_shipped},{r.units_sold},"
            f"{r.sold_per_sample:.2f}"
        )


def build_sample_csv(rows) -> bytes:
    """The samples-by-SKU CSV — identical columns/format to /samples-by-sku.csv."""
    lines = [",".join(_HEADERS)]
    lines.extend(_csv_rows(rows))
    return ("\n".join(lines) + "\n").encode()


def render_sample_email(rows, *, title_suffix: str):
    """(subject, html, text). HTML totals + by-SKU table from the same rows."""
    subject = f"Smashbox Sample Report — {title_suffix}"
    total_samples = sum(r.samples_sent for r in rows)
    total_orders = sum(r.sample_orders_shipped for r in rows)
    total_units_sold = sum(r.units_sold for r in rows)
    totals = (
        f'<div style="{H_SUB}">Samples Sent {total_samples} · '
        f'Orders Shipped {total_orders} · Units Sold {total_units_sold}</div>'
    )
    body_rows = []
    for r in rows:
        body_rows.append(
            f'<tr><td style="{TD}">{escape(r.sku_code or "Unmapped")}</td>'
            f'<td style="{TD}">{escape(r.name or "")}</td>'
            f'<td style="{TD}">{escape(r.tiktok_sku_id or "")}</td>'
            f'<td style="{TD_R}">{r.samples_sent}</td>'
            f'<td style="{TD_R}">{r.sample_orders_shipped}</td>'
            f'<td style="{TD_R}">{r.units_sold}</td>'
            f'<td style="{TD_R}">{r.sold_per_sample:.2f}</td></tr>'
        )
    total_row = (
        f'<tr><td style="{TOT}" colspan="3">Total</td>'
        f'<td style="{TOT_R}">{total_samples}</td>'
        f'<td style="{TOT_R}">{total_orders}</td>'
        f'<td style="{TOT_R}">{total_units_sold}</td>'
        f'<td style="{TOT}"></td></tr>'
    )
    html = (
        f'<div style="{CARD}"><div style="{HEADER}">'
        f'<div style="{H_TITLE}">Smashbox Sample Report</div>'
        f'<div style="{H_SUB}">{escape(title_suffix)}</div>{totals}</div>'
        f'<table style="width:100%;border-collapse:collapse"><thead><tr>'
        f'<th style="{TH}">SKU</th><th style="{TH}">Product</th>'
        f'<th style="{TH}">TikTok SKU</th><th style="{TH_R}">Samples Sent</th>'
        f'<th style="{TH_R}">Orders Shipped</th><th style="{TH_R}">Units Sold</th>'
        f'<th style="{TH_R}">Sold/Sample</th></tr></thead>'
        f'<tbody>{"".join(body_rows)}{total_row}</tbody></table></div>'
    )
    text_lines = [f"Smashbox Sample Report — {title_suffix}",
                  f"Samples Sent {total_samples} · Orders Shipped {total_orders} · "
                  f"Units Sold {total_units_sold}", ""]
    for r in rows:
        text_lines.append(
            f"{(r.sku_code or 'Unmapped'):<16}{r.samples_sent:>8}{r.units_sold:>8}"
        )
    return subject, html, "\n".join(text_lines)


def send_sample_report(db: Session, *, recipients: list[str], period: PeriodKind,
                       year, month, start_year, start_month, end_year,
                       end_month) -> None:
    """Resolve the by-SKU rows for the given scope (the same window the existing
    /samples-by-sku.csv uses), render, attach the CSV, send."""
    if not recipients:
        raise ValueError("no recipients configured for the sample report")
    view = compute_pnl_view(
        db, period, year, month,
        start_year=start_year, start_month=start_month,
        end_year=end_year, end_month=end_month,
    )
    start, end = window_for(view)
    rows = samples_by_sku_shipped(db, start, end)
    subject, html, text = render_sample_email(rows, title_suffix=view.title_suffix)
    csv_bytes = build_sample_csv(rows)
    fname = f"smashbox_samples_{view.year}"
    if view.month:
        fname += f"-{view.month:02d}"
    fname += ".csv"
    mailer.send_email(subject, text, to=recipients, html=html,
                      attachments=[(fname, csv_bytes, "csv")])
