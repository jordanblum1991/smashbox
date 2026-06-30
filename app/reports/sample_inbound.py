"""Inbound (on-order) sample units — incoming sample stock not yet received.

A unit is "inbound" while its `SampleInboundOrder` is **open** (not yet received).
Once received, SAP's SBS warehouse picks the stock up as on-hand, so the order
clears from here — inbound never double-counts the SAP-fed on-hand.

Mirrors `app/reports/in_transit.py`: each line's quantity is emitted under ALL of
its catalog SKU's identifiers (tiktok_sku_id / sku / tiktok_alt_sku) so it matches
whichever key a report uses for that SKU, without double-counting.
"""
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
from app.models.sku import Sku


def compute_sample_inbound(db: Session) -> dict[str, int]:
    """{sku_key: units_inbound} across all OPEN (un-received) sample inbound orders."""
    rows = db.execute(
        select(SampleInboundOrderLine.sku, SampleInboundOrderLine.quantity)
        .join(SampleInboundOrder,
              SampleInboundOrder.id == SampleInboundOrderLine.sample_inbound_order_id)
        .where(SampleInboundOrder.status == "open")
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


def sample_inbound_summary(db: Session) -> dict:
    """{open_orders, units_inbound} — open order count + total units on order. Sums
    line quantities directly (NOT compute_sample_inbound, which replicates each qty
    under every SKU identifier and would over-count here)."""
    rows = db.execute(
        select(
            SampleInboundOrder.id,
            func.coalesce(func.sum(SampleInboundOrderLine.quantity), 0),
        )
        .join(SampleInboundOrderLine,
              SampleInboundOrderLine.sample_inbound_order_id == SampleInboundOrder.id)
        .where(SampleInboundOrder.status == "open")
        .group_by(SampleInboundOrder.id)
    ).all()
    return {"open_orders": len(rows), "units_inbound": int(sum(r[1] for r in rows))}
