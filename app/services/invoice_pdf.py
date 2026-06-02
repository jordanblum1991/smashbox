"""WeasyPrint wrapper for invoice PDF generation.

The same Jinja template (`invoices/invoice_pdf.html`) renders both the
browser preview and the downloaded PDF — this module is the only place
WeasyPrint is referenced, so the import stays lazy (inside the function)
to keep app boot fast and pytest collection independent of whether the
WeasyPrint system libs (libpango / libcairo / etc.) are installed.

The HTML template uses plain CSS in a `<style>` block, not Tailwind, so
WeasyPrint doesn't need any external CSS resolution — `base_url` is set
to the request's base URL anyway in case future templates reference
static assets.
"""
from fastapi import Request

from app.models.invoice import Invoice
from app.templating import templates


def render_invoice_pdf(invoice: Invoice, request: Request) -> bytes:
    """Render the invoice template as a PDF byte string."""
    # Lazy import so the module imports cleanly even if WeasyPrint's
    # system libs aren't present (e.g. on a dev machine that hasn't yet
    # installed libpango). The PDF route is the only caller and gets a
    # clear ImportError if WeasyPrint truly isn't available.
    from weasyprint import HTML

    html_str = templates.get_template("invoices/invoice_pdf.html").render(
        {"invoice": invoice, "request": request}
    )
    return HTML(string=html_str, base_url=str(request.base_url)).write_pdf()
