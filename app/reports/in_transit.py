"""In-transit units — what's on order but not yet received, fed back into Demand
Planning as expected receipts so the planner stops re-recommending SKUs you've
already ordered.

A unit is "in transit" while its purchase order is **placed** (not draft, not
yet received). Each PO line's quantity is emitted under ALL of its catalog SKU's
identifiers (tiktok_sku_id / sku / tiktok_alt_sku), so it matches whichever key
the planner uses for that SKU — without double-counting (the planner reads each
canonical row once).
"""
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.sku import Sku


def compute_in_transit(db: Session) -> dict[str, int]:
    """{sku_key: units_on_order} across all PLACED (un-received) purchase orders."""
    rows = db.execute(
        select(PurchaseOrderLine.sku, PurchaseOrderLine.quantity)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(PurchaseOrder.status == "placed")
    ).all()
    if not rows:
        return {}

    sku_by_key: dict[str, Sku] = {}
    for s in db.execute(select(Sku)).scalars():
        for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if k:
                sku_by_key.setdefault(str(k).strip(), s)

    out: dict[str, int] = defaultdict(int)
    for line_sku, qty in rows:
        key = (line_sku or "").strip()
        if not key:
            continue
        s = sku_by_key.get(key)
        idents = (
            [str(k).strip() for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku) if k]
            if s else [key]
        )
        for ident in set(idents):
            out[ident] += int(qty or 0)
    return dict(out)


def in_transit_summary(db: Session) -> dict:
    """Roll-up for the planner header: how many PLACED purchase orders are open
    and the total units on order. Sums line quantities directly (NOT
    compute_in_transit, which replicates each qty under every SKU identifier and
    would over-count here)."""
    rows = db.execute(
        select(
            PurchaseOrder.id,
            func.coalesce(func.sum(PurchaseOrderLine.quantity), 0),
        )
        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .where(PurchaseOrder.status == "placed")
        .group_by(PurchaseOrder.id)
    ).all()
    return {"open_pos": len(rows), "units_on_order": int(sum(r[1] for r in rows))}
