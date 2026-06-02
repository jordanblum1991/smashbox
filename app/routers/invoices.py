"""Invoice generation — admin-only CRUD on /admin/invoices.

Seven routes:
  - GET  /admin/invoices                   list view
  - GET  /admin/invoices/new               create form
  - POST /admin/invoices                   create + redirect to detail
  - GET  /admin/invoices/{id}              detail view (iframe-embeds preview)
  - GET  /admin/invoices/{id}/preview      bare invoice HTML (for the iframe)
  - GET  /admin/invoices/{id}/pdf          WeasyPrint-rendered PDF download
  - POST /admin/invoices/{id}/mark-paid    flip status to "paid" (idempotent)

Form errors flash via a 303 redirect back to /admin/invoices/new with the
error reason AND every submitted field preserved as query params, so the
user doesn't lose what they typed. Same pattern as _credit_error_redirect
in app/routers/gmv_max_reimbursements.py.

Invoice number is unique. The create form pre-fills with the next
suggested number (max suffix + 1, or "OL-2026-007" if no invoices exist)
but the field is editable so finance can issue out-of-band numbers.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models.invoice import Invoice
from app.services.invoice_pdf import render_invoice_pdf
from app.templating import templates

router = APIRouter(tags=["invoices"])


# Default Bill To block pre-filled on the create form. Editable per invoice
# so customers other than Smashbox can be billed without a code change.
_DEFAULT_BILL_TO = (
    "Smashbox Beauty Cosmetics\n"
    "7 Corporate Center Drive\n"
    "Melville, NY 11747"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _suggest_next_number(db: Session) -> str:
    """Suggested number for the next invoice.

    Strategy: pick the lexicographically largest existing number (works
    because the format is `OL-YYYY-NNN` with zero-padded NNN), parse its
    suffix, bump by 1. Keep the year of the max. Fallback to OL-2026-007
    when no invoices exist — per user, OL-2026-005 and OL-2026-006 were
    issued externally before this feature shipped, so 007 is the start.
    """
    last = db.execute(
        select(Invoice.number).order_by(Invoice.number.desc()).limit(1)
    ).scalar_one_or_none()
    if last is None:
        return "OL-2026-007"
    try:
        prefix, year, suffix = last.split("-")
        return f"{prefix}-{year}-{int(suffix) + 1:03d}"
    except (ValueError, AttributeError):
        # Unexpected format on a manually-edited number — fall back to default.
        return "OL-2026-007"


def _new_error_redirect(reason: str, **form_values: str) -> RedirectResponse:
    """303 back to /admin/invoices/new with error + every submitted field
    preserved so the form re-renders with the user's input intact."""
    params: dict[str, str] = {"error": reason}
    for k, v in form_values.items():
        if v is not None and v != "":
            params[k] = v
    return RedirectResponse(
        f"/admin/invoices/new?{urlencode(params)}", status_code=303
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/admin/invoices", dependencies=[Depends(require_admin)])
def invoices_list(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
) -> Response:
    invoices = db.execute(
        select(Invoice).order_by(Invoice.created_at.desc())
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/invoices_list.html",
        {"invoices": invoices, "error": error, "notice": notice},
    )


@router.get("/admin/invoices/new", dependencies=[Depends(require_admin)])
def invoice_new_form(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    # Preserved-on-error form values. All optional; absence means use defaults.
    number: str | None = None,
    issue_date: str | None = None,
    description_preset: str | None = None,
    description_headline: str | None = None,
    description_subtitle: str | None = None,
    period_label: str | None = None,
    bill_to_block: str | None = None,
    amount: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/invoices_new.html",
        {
            "error": error,
            "number": number or _suggest_next_number(db),
            "issue_date": issue_date or date.today().isoformat(),
            "description_preset": description_preset or "ad_spend",
            "description_headline": description_headline or "",
            "description_subtitle": description_subtitle or "",
            "period_label": period_label or "",
            "bill_to_block": bill_to_block or _DEFAULT_BILL_TO,
            "amount": amount or "",
        },
    )


@router.post("/admin/invoices", dependencies=[Depends(require_admin)])
def invoice_create(
    request: Request,
    number: str = Form(...),
    issue_date: str = Form(...),
    description_preset: str = Form(...),
    description_headline: str = Form(...),
    description_subtitle: str = Form(""),
    period_label: str = Form(""),
    bill_to_block: str = Form(...),
    amount: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    # Preserve every field so an error redirect can re-fill the form.
    submitted = {
        "number": number,
        "issue_date": issue_date,
        "description_preset": description_preset,
        "description_headline": description_headline,
        "description_subtitle": description_subtitle,
        "period_label": period_label,
        "bill_to_block": bill_to_block,
        "amount": amount,
    }

    # ----- Validation -----
    number_clean = (number or "").strip()
    if not number_clean:
        return _new_error_redirect("Invoice number is required.", **submitted)

    # Parse issue_date (HTML5 date input emits ISO YYYY-MM-DD).
    try:
        issue_date_obj = date.fromisoformat(issue_date)
    except (TypeError, ValueError):
        return _new_error_redirect(
            "Issue date is required and must be a valid date.", **submitted
        )

    headline_clean = (description_headline or "").strip()
    if not headline_clean:
        return _new_error_redirect(
            "Description headline is required.", **submitted
        )

    bill_to_clean = (bill_to_block or "").strip()
    if not bill_to_clean:
        return _new_error_redirect("Bill To block is required.", **submitted)

    # Amount must parse and be strictly positive.
    try:
        amount_dec = Decimal((amount or "").strip())
    except (InvalidOperation, AttributeError):
        return _new_error_redirect(
            f"Amount '{amount}' is not a valid number.", **submitted
        )
    if amount_dec <= 0:
        return _new_error_redirect(
            "Amount must be greater than $0.00.", **submitted
        )
    # Quantize to cents to match the column type.
    amount_dec = amount_dec.quantize(Decimal("0.01"))

    # Number uniqueness. Pre-check for a friendly error message; the DB's
    # UNIQUE constraint is the backstop if two requests race.
    existing = db.execute(
        select(Invoice).where(Invoice.number == number_clean)
    ).scalar_one_or_none()
    if existing is not None:
        return _new_error_redirect(
            f"Invoice number {number_clean!r} is already in use.", **submitted
        )

    # ----- Persist -----
    inv = Invoice(
        number=number_clean,
        issue_date=issue_date_obj,
        bill_to_block=bill_to_clean,
        description_headline=headline_clean,
        description_subtitle=(description_subtitle or "").strip() or None,
        period_label=(period_label or "").strip() or None,
        amount=amount_dec,
        status="issued",
        brand_code="SMASHBOX",
    )
    db.add(inv)
    db.commit()

    return RedirectResponse(
        f"/admin/invoices/{inv.id}?{urlencode({'notice': 'Invoice created.'})}",
        status_code=303,
    )


@router.get("/admin/invoices/{invoice_id}", dependencies=[Depends(require_admin)])
def invoice_detail(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
    notice: str | None = None,
) -> Response:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return RedirectResponse(
            f"/admin/invoices?{urlencode({'error': 'Invoice not found.'})}",
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "admin/invoices_detail.html",
        {"invoice": inv, "notice": notice},
    )


@router.get(
    "/admin/invoices/{invoice_id}/preview", dependencies=[Depends(require_admin)]
)
def invoice_preview(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Bare HTML invoice document — no app chrome — for the iframe on the
    detail page. Same template the PDF generator uses, so the preview is
    visually identical to the downloaded PDF."""
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return HTMLResponse("Invoice not found.", status_code=404)
    return templates.TemplateResponse(
        request,
        "invoices/invoice_pdf.html",
        {"invoice": inv},
    )


@router.get("/admin/invoices/{invoice_id}/pdf", dependencies=[Depends(require_admin)])
def invoice_pdf_download(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return Response("Invoice not found.", status_code=404)
    pdf_bytes = render_invoice_pdf(inv, request)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{inv.number}.pdf"',
        },
    )


@router.post(
    "/admin/invoices/{invoice_id}/mark-paid", dependencies=[Depends(require_admin)]
)
def invoice_mark_paid(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Flip status to 'paid'. Idempotent: a second call on an already-paid
    invoice is a no-op (no error, no commit needed)."""
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return RedirectResponse(
            f"/admin/invoices?{urlencode({'error': 'Invoice not found.'})}",
            status_code=303,
        )
    if inv.status != "paid":
        inv.status = "paid"
        db.commit()
        notice = f"Invoice {inv.number} marked paid."
    else:
        notice = f"Invoice {inv.number} is already paid."
    return RedirectResponse(
        f"/admin/invoices/{inv.id}?{urlencode({'notice': notice})}",
        status_code=303,
    )
