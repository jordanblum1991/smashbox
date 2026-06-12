"""Data-health check: catalog SKUs with no unit COGS that are actually in play.

A SKU with `unit_cogs` 0/NULL silently undervalues inventory (the Inventory
Value report multiplies on-hand × COGS) and breaks per-SKU margin. We only flag
SKUs that MATTER right now — ones with current sellable on-hand OR recorded sales
— so a dormant catalog row without a cost doesn't create noise.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class MissingCogsRow:
    sku_code: str
    name: str | None
    on_hand: int
    units_sold: int


def _latest_sellable_on_hand(db: Session) -> dict[str, int]:
    """Most-recent on_hand per snapshot SKU key (sellable inventory)."""
    latest = dict(db.execute(
        select(InventorySnapshot.sku, func.max(InventorySnapshot.captured_at))
        .group_by(InventorySnapshot.sku)
    ).all())
    if not latest:
        return {}
    rows = db.execute(
        select(InventorySnapshot.sku, InventorySnapshot.on_hand, InventorySnapshot.captured_at)
        .where(InventorySnapshot.sku.in_(latest.keys()))
    ).all()
    return {sku: int(oh or 0) for sku, oh, cap in rows if latest[sku] == cap}


def _units_sold(db: Session) -> dict[str, int]:
    """Units sold per OrderLine.sku across PAID orders."""
    rows = db.execute(
        select(OrderLine.sku, func.sum(OrderLine.quantity))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .group_by(OrderLine.sku)
    ).all()
    return {sku: int(q or 0) for sku, q in rows if sku}


def find_missing_cogs(db: Session) -> list[MissingCogsRow]:
    zero = db.execute(
        select(Sku).where((Sku.unit_cogs == 0) | (Sku.unit_cogs.is_(None)))
    ).scalars().all()
    if not zero:
        return []
    on_hand = _latest_sellable_on_hand(db)
    sold = _units_sold(db)

    out: list[MissingCogsRow] = []
    for s in zero:
        # One snapshot/sales entry per SKU — take the first key that matches,
        # don't sum across keys (which would double-count when keys coincide).
        keys = [str(k).strip() for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku) if k]
        oh = next((on_hand[k] for k in keys if k in on_hand), 0)
        us = next((sold[k] for k in keys if k in sold), 0)
        if oh > 0 or us > 0:
            out.append(MissingCogsRow(sku_code=s.sku, name=s.name, on_hand=oh, units_sold=us))
    out.sort(key=lambda r: (-(r.on_hand + r.units_sold), r.sku_code or ""))
    return out


def count_missing_cogs(db: Session) -> int:
    """Cheap-ish count for the Data Health badge."""
    return len(find_missing_cogs(db))
