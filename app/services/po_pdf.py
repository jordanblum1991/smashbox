"""WeasyPrint wrapper for purchase-order PDF generation.

Mirrors app/services/invoice_pdf.py: the same Jinja template
(`purchase_orders/po_pdf.html`) renders both the browser preview and the
downloaded PDF. WeasyPrint is imported lazily so app boot / pytest collection
don't depend on its system libs being installed.
"""
from fastapi import Request

from app.models.purchase_order import PurchaseOrder
from app.templating import templates


def render_po_pdf(po: PurchaseOrder, request: Request) -> bytes:
    """Render the purchase-order template as a PDF byte string."""
    from weasyprint import HTML

    html_str = templates.get_template("purchase_orders/po_pdf.html").render(
        {"po": po, "request": request}
    )
    return HTML(string=html_str, base_url=str(request.base_url)).write_pdf()
