"""Admin CRUD for Smashbox Product Invoices — the inbound AP ledger.

Manual entry of invoices received from Smashbox for sellable-inventory
purchases, plus credits applied against each. Mirrors the validation / 303-flash
discipline of app/routers/gmv_max_reimbursements.py. Net owed = amount − credits;
status is open|paid. Standalone — does not feed the P&L.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_admin
from app.db import get_db
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceCredit
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    qs: dict[str, str] = {}
    if error:
        qs["error"] = error
    if notice:
        qs["notice"] = notice
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(f"/admin/product-invoices{suffix}", status_code=303)


def _parse_date(raw: str, label: str) -> tuple[date | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, f"{label} is required."
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, f"{label} must be a valid date (YYYY-MM-DD)."


def _parse_amount(raw: str, label: str) -> tuple[Decimal | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, f"{label} is required."
    try:
        amt = Decimal(raw)
    except InvalidOperation:
        return None, f"{label} must be a number (got {raw!r})."
    if amt <= 0:
        return None, f"{label} must be greater than 0."
    return amt, None


@router.get("/product-invoices", dependencies=[Depends(require_admin)])
def product_invoices_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    invoices = db.execute(
        select(PurchaseInvoice)
        .options(selectinload(PurchaseInvoice.credits))
        .order_by(PurchaseInvoice.invoice_date.desc(), PurchaseInvoice.id.desc())
    ).scalars().all()

    total_billed = sum((i.amount for i in invoices), Decimal("0"))
    total_credits = sum((i.credits_total for i in invoices), Decimal("0"))
    total_net = total_billed - total_credits
    open_net = sum((i.net_owed for i in invoices if i.status != "paid"), Decimal("0"))

    return templates.TemplateResponse(
        request,
        "admin/purchase_invoices.html",
        {
            "invoices": invoices,
            "total_billed": total_billed,
            "total_credits": total_credits,
            "total_net": total_net,
            "open_net": open_net,
            "error": error,
            "notice": notice,
        },
    )


@router.post("/product-invoices", dependencies=[Depends(require_admin)])
def create_purchase_invoice(
    number: str = Form(...),
    invoice_date: str = Form(...),
    amount: str = Form(...),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    num = (number or "").strip()
    if not num:
        return _back(error="Invoice number is required.")
    d, err = _parse_date(invoice_date, "Invoice date")
    if err:
        return _back(error=err)
    amt, err = _parse_amount(amount, "Amount")
    if err:
        return _back(error=err)
    exists = db.execute(
        select(PurchaseInvoice).where(PurchaseInvoice.number == num)
    ).scalar_one_or_none()
    if exists is not None:
        return _back(error=f"Invoice {num!r} already exists.")
    db.add(PurchaseInvoice(
        number=num, invoice_date=d, amount=amt, status="open",
        note=((note or "").strip() or None),
    ))
    db.commit()
    return _back(notice=f"Added invoice {num}.")


@router.post("/product-invoices/{invoice_id}/credits", dependencies=[Depends(require_admin)])
def add_purchase_credit(
    invoice_id: int,
    credit_date: str = Form(...),
    amount: str = Form(...),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    inv = db.get(PurchaseInvoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    d, err = _parse_date(credit_date, "Credit date")
    if err:
        return _back(error=err)
    amt, err = _parse_amount(amount, "Credit amount")
    if err:
        return _back(error=err)
    db.add(PurchaseInvoiceCredit(
        purchase_invoice_id=inv.id, credit_date=d, amount=amt,
        reason=((reason or "").strip() or None),
    ))
    db.commit()
    return _back(notice=f"Added ${amt} credit to {inv.number}.")


@router.post("/product-invoices/{invoice_id}/credits/{credit_id}/delete", dependencies=[Depends(require_admin)])
def delete_purchase_credit(invoice_id: int, credit_id: int, db: Session = Depends(get_db)):
    credit = db.get(PurchaseInvoiceCredit, credit_id)
    if credit is None or credit.purchase_invoice_id != invoice_id:
        raise HTTPException(status_code=404, detail="Credit not found")
    db.delete(credit)
    db.commit()
    return _back(notice="Credit removed.")


@router.post("/product-invoices/{invoice_id}/status", dependencies=[Depends(require_admin)])
def set_purchase_invoice_status(
    invoice_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    inv = db.get(PurchaseInvoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    new_status = (status or "").strip().lower()
    if new_status not in ("open", "paid"):
        return _back(error=f"Invalid status {status!r}.")
    inv.status = new_status
    db.commit()
    return _back(notice=f"{inv.number} marked {new_status}.")


@router.post("/product-invoices/{invoice_id}/delete", dependencies=[Depends(require_admin)])
def delete_purchase_invoice(invoice_id: int, db: Session = Depends(get_db)):
    inv = db.get(PurchaseInvoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    number = inv.number
    db.delete(inv)   # cascades to credits
    db.commit()
    return _back(notice=f"Deleted invoice {number}.")
