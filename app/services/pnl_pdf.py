"""WeasyPrint wrapper for the P&L statement PDF — mirrors `invoice_pdf.py`.

Renders `reports/pnl_pdf.html` (plain CSS in a `<style>` block, not Tailwind) to
PDF bytes. The WeasyPrint import stays lazy so this module imports cleanly even
where its system libs (libpango / libcairo) aren't installed; the
`/export/pnl.pdf` route is the only caller and surfaces a clear ImportError if
WeasyPrint truly isn't available.
"""
from fastapi import Request

from app.reports.pnl import PnLView
from app.reports.pnl_statement import statement_lines
from app.templating import templates


def render_pnl_pdf(view: PnLView, request: Request) -> bytes:
    """Render the P&L statement template as a PDF byte string."""
    from weasyprint import HTML

    html_str = templates.get_template("reports/pnl_pdf.html").render(
        {"view": view, "lines": statement_lines(view.total), "request": request}
    )
    return HTML(string=html_str, base_url=str(request.base_url)).write_pdf()
