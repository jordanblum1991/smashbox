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
from sqlalchemy.orm import Session, selectinload

from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.services.sku_alias import load_alias_map


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


def likely_received_order_ids(db: Session, *, alias_map: dict[str, str] | None = None) -> set[int]:
    """Open inbound orders whose SBS on-hand appears to have grown since the order
    was logged — i.e. SAP has probably already counted the stock as on-hand.

    Advisory only: a HINT to confirm receipt (so on-hand + inbound stop
    double-counting), not an auto-action — the buyer still clicks "received".
    For each open order, an SKU is "likely received" when there's a sample
    snapshot dated AFTER the order's created_at whose on-hand exceeds the on-hand
    at order-creation time. SKUs are alias-collapsed to match the report."""
    open_orders = db.execute(
        select(SampleInboundOrder)
        .options(selectinload(SampleInboundOrder.lines))
        .where(SampleInboundOrder.status == "open")
    ).scalars().all()
    if not open_orders:
        return set()

    alias_map = alias_map if alias_map is not None else load_alias_map(db)

    # On-hand timeline per canonical SKU: sorted [(captured_at, on_hand), ...].
    timeline: dict[str, list] = defaultdict(list)
    for sku, oh, cap in db.execute(
        select(SampleInventorySnapshot.sku, SampleInventorySnapshot.on_hand,
               SampleInventorySnapshot.captured_at)
    ).all():
        timeline[alias_map.get(sku, sku)].append((cap, int(oh or 0)))
    for k in timeline:
        timeline[k].sort()

    flagged: set[int] = set()
    for o in open_orders:
        for ln in o.lines:
            tl = timeline.get(alias_map.get(ln.sku, ln.sku))
            if not tl:
                continue
            baseline = 0
            for cap, oh in tl:
                if cap <= o.created_at:
                    baseline = oh
            latest_cap, latest_oh = tl[-1]
            if latest_cap > o.created_at and latest_oh > baseline:
                flagged.add(o.id)
                break
    return flagged


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
