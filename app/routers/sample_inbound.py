"""Admin CRUD for inbound sample orders — incoming sample stock.

Lightweight (no draft/PDF, unlike sellable Purchase Orders): create an order with
a few SKU+qty lines, and it counts as inbound (on-order) sample inventory while
OPEN. Marking it RECEIVED clears it — SAP's SBS warehouse owns on-hand at that
point, so we never double-count. Sample stock is $0; there is no cost field.
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_admin
from app.db import get_db
from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
from app.models.sku import Sku
from app.reports.sample_inbound import sample_inbound_summary
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get_or_404(db: Session, order_id: int) -> SampleInboundOrder:
    o = db.execute(
        select(SampleInboundOrder).where(SampleInboundOrder.id == order_id)
        .options(selectinload(SampleInboundOrder.lines))
    ).scalar_one_or_none()
    if o is None:
        raise HTTPException(status_code=404, detail="sample inbound order not found")
    return o


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    qs = f"?error={error}" if error else (f"?notice={notice}" if notice else "")
    return RedirectResponse(f"/admin/sample-inbound{qs}", status_code=303)


@router.get("/sample-inbound", dependencies=[Depends(require_admin)])
def sample_inbound_list(request: Request, db: Session = Depends(get_db),
                        error: str | None = None, notice: str | None = None):
    orders = db.execute(
        select(SampleInboundOrder).options(selectinload(SampleInboundOrder.lines))
        .order_by(SampleInboundOrder.id.desc())
    ).scalars().all()
    # Distinct SKU codes for the line pickers (sku.sku isn't unique — collapse).
    seen: dict[str, str] = {}
    for code, name in db.execute(select(Sku.sku, Sku.name).order_by(Sku.sku)).all():
        seen.setdefault(code, name)
    sku_options = sorted(seen.items())
    return templates.TemplateResponse(
        request, "admin/sample_inbound.html",
        {"orders": orders, "sku_options": sku_options,
         "summary": sample_inbound_summary(db),
         "error": error, "notice": notice},
    )


@router.post("/sample-inbound", dependencies=[Depends(require_admin)])
def sample_inbound_create(
    source: str = Form(default=""),
    note: str = Form(default=""),
    sku: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    name_by_code = {code: name for code, name in db.execute(select(Sku.sku, Sku.name)).all()}
    order = SampleInboundOrder(source=source.strip() or None, note=note.strip() or None, status="open")
    db.add(order)
    db.flush()
    added = 0
    for code, qty_raw in zip(sku, quantity):
        code = (code or "").strip()
        if not code:
            continue
        try:
            qty = int((qty_raw or "").strip())
        except ValueError:
            continue
        if qty <= 0:
            continue
        db.add(SampleInboundOrderLine(
            sample_inbound_order_id=order.id, sku=code,
            name=name_by_code.get(code), quantity=qty))
        added += 1
    if not added:
        db.rollback()
        return _back(error="Add at least one line (SKU + a quantity greater than 0).")
    db.commit()
    return _back(notice=f"Inbound sample order created with {added} line{'' if added == 1 else 's'}.")


@router.post("/sample-inbound/{order_id}/receive", dependencies=[Depends(require_admin)])
def sample_inbound_receive(order_id: int, db: Session = Depends(get_db)):
    order = _get_or_404(db, order_id)
    if not order.is_open:
        return _back(error="Only an open order can be received.")
    order.status = "received"
    order.received_at = _utcnow()
    db.commit()
    return _back(notice="Marked received — cleared from inbound (SAP on-hand now counts it).")


@router.post("/sample-inbound/{order_id}/unreceive", dependencies=[Depends(require_admin)])
def sample_inbound_unreceive(order_id: int, db: Session = Depends(get_db)):
    order = _get_or_404(db, order_id)
    if not order.is_received:
        return _back(error="Only a received order can be reopened.")
    order.status = "open"
    order.received_at = None
    db.commit()
    return _back(notice="Reopened — counting as inbound again.")


@router.post("/sample-inbound/{order_id}/delete", dependencies=[Depends(require_admin)])
def sample_inbound_delete(order_id: int, db: Session = Depends(get_db)):
    order = _get_or_404(db, order_id)
    db.delete(order)
    db.commit()
    return _back(notice="Inbound sample order deleted.")
