"""Unmapped SKUs report.

Lists every distinct OrderLine.sku that does NOT appear in the catalog
(neither in the SKU master nor the bundle mapping). Use this to figure out
which TikTok SKU IDs need to be added so COGS, product names, and the
sample-tracking table fill in correctly.

Scope: covers PAID, SAMPLE, and PAID_SAMPLE order lines — anything that ships
to a customer counts as something we should be able to identify. The
`purpose` column on each row breaks out where the units came from.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class UnmappedRow:
    identifier: str
    units: int
    gross: Decimal
    line_count: int
    first_seen: datetime
    last_seen: datetime
    paid_units: int      # units from PAID orders
    sample_units: int    # units from SAMPLE / PAID_SAMPLE orders


def _catalog_keys(db: Session) -> set[str]:
    keys: set[str] = set()
    for s in db.execute(select(Sku)).scalars():
        for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if k:
                keys.add(str(k).strip())
    for b in db.execute(select(Bundle)).scalars():
        for k in (b.tiktok_sku_id, b.bundle_sku):
            if k:
                keys.add(str(k).strip())
    return keys


def find_unmapped_skus(db: Session) -> list[UnmappedRow]:
    catalog = _catalog_keys(db)

    stmt = (
        select(
            OrderLine.sku,
            func.sum(OrderLine.quantity).label("units"),
            func.sum(OrderLine.gross_sales).label("gross"),
            func.count(OrderLine.id).label("lines"),
            func.min(Order.placed_at).label("first"),
            func.max(Order.placed_at).label("last"),
            func.sum(case((Order.order_type == OrderType.PAID, OrderLine.quantity), else_=0)).label("paid_units"),
            func.sum(case(
                (Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]), OrderLine.quantity),
                else_=0,
            )).label("sample_units"),
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        .group_by(OrderLine.sku)
        .order_by(func.sum(OrderLine.quantity).desc())
    )

    out: list[UnmappedRow] = []
    for r in db.execute(stmt):
        key = (r.sku or "").strip()
        if not key or key in catalog:
            continue
        out.append(UnmappedRow(
            identifier=key,
            units=int(r.units or 0),
            gross=Decimal(str(r.gross or 0)),
            line_count=int(r.lines or 0),
            first_seen=r.first,
            last_seen=r.last,
            paid_units=int(r.paid_units or 0),
            sample_units=int(r.sample_units or 0),
        ))
    return out


def count_unmapped_skus(db: Session) -> int:
    """Cheap count for the nav-bar Data Health badge."""
    catalog = _catalog_keys(db)
    if not catalog:
        # No catalog yet ⇒ everything is technically unmapped, but flagging that
        # before any SKU master is loaded would be misleading. Stay quiet.
        return 0
    keys = db.execute(
        select(OrderLine.sku)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        .group_by(OrderLine.sku)
    ).scalars()
    return sum(1 for k in keys if k and k.strip() and k.strip() not in catalog)
