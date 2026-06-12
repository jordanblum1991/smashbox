"""Admin CRUD for Purchase Orders.

Seed a DRAFT PO from the demand planner's reorder recommendations, edit it
(quantities, add/remove items, supplier, notes), then PLACE it — which freezes
the PO and exposes a WeasyPrint PDF to send to the supplier. Mirrors the
validation / 303-flash discipline of app/routers/purchase_invoices.py.
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_admin
from app.config import settings
from app.db import get_db
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.sku import Sku
from app.reports.demand_planning import compute_demand_planning_view
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _next_po_number(db: Session) -> str:
    """Next sequential PO-#### number."""
    mx = 0
    for n in db.execute(select(PurchaseOrder.number)).scalars().all():
        m = re.match(r"PO-(\d+)", n or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return f"PO-{mx + 1:04d}"


def _get_or_404(db: Session, po_id: int) -> PurchaseOrder:
    po = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == po_id)
        .options(selectinload(PurchaseOrder.lines))
    ).scalar_one_or_none()
    if po is None:
        raise HTTPException(status_code=404, detail="purchase order not found")
    return po


def _back_detail(po_id: int, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    qs = ""
    if error:
        qs = f"?error={error}"
    elif notice:
        qs = f"?notice={notice}"
    return RedirectResponse(f"/admin/purchase-orders/{po_id}{qs}", status_code=303)


@router.get("/purchase-orders", dependencies=[Depends(require_admin)])
def purchase_orders_list(request: Request, db: Session = Depends(get_db),
                         error: str | None = None, notice: str | None = None):
    orders = db.execute(
        select(PurchaseOrder).options(selectinload(PurchaseOrder.lines))
        .order_by(PurchaseOrder.id.desc())
    ).scalars().all()
    return templates.TemplateResponse(
        request, "purchase_orders/list.html",
        {"orders": orders, "error": error, "notice": notice},
    )


@router.post("/purchase-orders/from-plan", dependencies=[Depends(require_admin)])
def purchase_order_from_plan(request: Request, db: Session = Depends(get_db)):
    """Create a draft PO seeded from the demand planner's reorder recommendations
    (every SKU with a suggested order quantity > 0)."""
    view = compute_demand_planning_view(db)
    po = PurchaseOrder(
        number=_next_po_number(db),
        supplier=settings.default_po_supplier,
        status="draft",
    )
    db.add(po)
    db.flush()
    seeded = 0
    for r in view.rows:
        if r.suggested_order_qty and r.suggested_order_qty > 0:
            qty = int(r.suggested_order_qty)
            unit_cost = (r.investment / qty).quantize(Decimal("0.0001")) if qty else Decimal("0")
            db.add(PurchaseOrderLine(
                purchase_order_id=po.id,
                sku=r.sku_code or r.component_sku,
                name=r.name,
                quantity=qty,
                unit_cost=unit_cost,
            ))
            seeded += 1
    db.commit()
    notice = (f"Created {po.number} with {seeded} item{'' if seeded == 1 else 's'} from the plan."
              if seeded else f"Created empty {po.number} — no reorder recommendations right now; add items below.")
    return _back_detail(po.id, notice=notice)


@router.post("/purchase-orders/blank", dependencies=[Depends(require_admin)])
def purchase_order_blank(db: Session = Depends(get_db)):
    """Create an empty draft PO to fill in manually."""
    po = PurchaseOrder(number=_next_po_number(db), supplier=settings.default_po_supplier, status="draft")
    db.add(po)
    db.commit()
    return _back_detail(po.id, notice=f"Created {po.number}.")


@router.get("/purchase-orders/{po_id}", dependencies=[Depends(require_admin)])
def purchase_order_detail(po_id: int, request: Request, db: Session = Depends(get_db),
                          error: str | None = None, notice: str | None = None):
    po = _get_or_404(db, po_id)
    skus = db.execute(select(Sku).order_by(Sku.sku)).scalars().all()
    return templates.TemplateResponse(
        request, "purchase_orders/detail.html",
        {"po": po, "skus": skus, "error": error, "notice": notice},
    )


def _require_draft(po: PurchaseOrder) -> None:
    if not po.is_draft:
        raise HTTPException(status_code=400, detail=f"purchase order is {po.status} (read-only)")


@router.post("/purchase-orders/{po_id}/edit", dependencies=[Depends(require_admin)])
def purchase_order_edit(po_id: int, supplier: str = Form(...), notes: str = Form(default=""),
                        db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    _require_draft(po)
    supplier = supplier.strip()
    if not supplier:
        return _back_detail(po_id, error="Supplier is required.")
    po.supplier = supplier
    po.notes = notes.strip() or None
    db.commit()
    return _back_detail(po_id, notice="Saved.")


@router.post("/purchase-orders/{po_id}/lines", dependencies=[Depends(require_admin)])
def purchase_order_add_line(po_id: int, sku_id: str = Form(...), quantity: str = Form(...),
                            db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    _require_draft(po)
    sku = db.get(Sku, int(sku_id)) if sku_id.isdigit() else None
    if sku is None:
        return _back_detail(po_id, error="Pick a SKU to add.")
    qty, err = _parse_int(quantity, "Quantity")
    if err:
        return _back_detail(po_id, error=err)
    db.add(PurchaseOrderLine(
        purchase_order_id=po.id, sku=sku.sku, name=sku.name,
        quantity=qty, unit_cost=sku.unit_cogs or Decimal("0"),
    ))
    db.commit()
    return _back_detail(po_id, notice=f"Added {sku.sku}.")


@router.post("/purchase-orders/{po_id}/lines/{line_id}/edit", dependencies=[Depends(require_admin)])
def purchase_order_edit_line(po_id: int, line_id: int, quantity: str = Form(...),
                             unit_cost: str = Form(...), db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    _require_draft(po)
    line = db.get(PurchaseOrderLine, line_id)
    if line is None or line.purchase_order_id != po.id:
        raise HTTPException(status_code=404, detail="line not found")
    qty, err = _parse_int(quantity, "Quantity")
    if err:
        return _back_detail(po_id, error=err)
    cost, err = _parse_decimal(unit_cost, "Unit cost")
    if err:
        return _back_detail(po_id, error=err)
    line.quantity = qty
    line.unit_cost = cost
    db.commit()
    return _back_detail(po_id, notice="Line updated.")


@router.post("/purchase-orders/{po_id}/lines/{line_id}/delete", dependencies=[Depends(require_admin)])
def purchase_order_delete_line(po_id: int, line_id: int, db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    _require_draft(po)
    line = db.get(PurchaseOrderLine, line_id)
    if line is not None and line.purchase_order_id == po.id:
        db.delete(line)
        db.commit()
    return _back_detail(po_id, notice="Item removed.")


@router.post("/purchase-orders/{po_id}/place", dependencies=[Depends(require_admin)])
def purchase_order_place(po_id: int, db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    if not po.lines:
        return _back_detail(po_id, error="Add at least one item before placing the PO.")
    from app.models.import_batch import _utc_now_naive
    po.status = "placed"
    po.placed_at = _utc_now_naive()
    db.commit()
    return _back_detail(po_id, notice=f"{po.number} placed. Download the PDF to send to your supplier.")


@router.post("/purchase-orders/{po_id}/reopen", dependencies=[Depends(require_admin)])
def purchase_order_reopen(po_id: int, db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    po.status = "draft"
    po.placed_at = None
    db.commit()
    return _back_detail(po_id, notice="Reopened for editing.")


@router.post("/purchase-orders/{po_id}/receive", dependencies=[Depends(require_admin)])
def purchase_order_receive(po_id: int, db: Session = Depends(get_db)):
    """Mark a placed PO as received — its units stop counting as in-transit, so
    Demand Planning recommends on the now-arrived stock again."""
    po = _get_or_404(db, po_id)
    if not po.is_placed:
        return _back_detail(po_id, error="Only a placed PO can be received.")
    po.status = "received"
    db.commit()
    return _back_detail(po_id, notice=f"{po.number} marked received — units cleared from in-transit.")


@router.post("/purchase-orders/{po_id}/unreceive", dependencies=[Depends(require_admin)])
def purchase_order_unreceive(po_id: int, db: Session = Depends(get_db)):
    """Undo a receive — back to placed (in-transit) without losing placed_at."""
    po = _get_or_404(db, po_id)
    if not po.is_received:
        return _back_detail(po_id, error="Only a received PO can be moved back to placed.")
    po.status = "placed"
    db.commit()
    return _back_detail(po_id, notice=f"{po.number} moved back to placed (in-transit).")


@router.post("/purchase-orders/{po_id}/delete", dependencies=[Depends(require_admin)])
def purchase_order_delete(po_id: int, db: Session = Depends(get_db)):
    po = _get_or_404(db, po_id)
    db.delete(po)
    db.commit()
    return RedirectResponse("/admin/purchase-orders?notice=Purchase+order+deleted.", status_code=303)


@router.get("/purchase-orders/{po_id}/pdf", dependencies=[Depends(require_admin)])
def purchase_order_pdf(po_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    po = _get_or_404(db, po_id)
    from app.services.po_pdf import render_po_pdf
    pdf_bytes = render_po_pdf(po, request)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{po.number}.pdf"'},
    )


@router.get("/purchase-orders/{po_id}/csv", dependencies=[Depends(require_admin)])
def purchase_order_csv(po_id: int, db: Session = Depends(get_db)) -> Response:
    po = _get_or_404(db, po_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sku", "product", "quantity", "unit_cost", "line_total"])
    for ln in po.lines:
        w.writerow([ln.sku, ln.name or "", ln.quantity, f"{ln.unit_cost:.4f}", f"{ln.line_total:.2f}"])
    w.writerow([])
    w.writerow(["", "", po.unit_count, "Total", f"{po.total:.2f}"])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{po.number}.csv"'},
    )


def _parse_int(raw: str, label: str) -> tuple[int, str | None]:
    try:
        v = int((raw or "").strip())
    except ValueError:
        return 0, f"{label} must be a whole number."
    if v <= 0:
        return 0, f"{label} must be greater than 0."
    return v, None


def _parse_decimal(raw: str, label: str) -> tuple[Decimal, str | None]:
    try:
        v = Decimal((raw or "").strip())
    except InvalidOperation:
        return Decimal("0"), f"{label} must be a number."
    if v < 0:
        return Decimal("0"), f"{label} can't be negative."
    return v, None
