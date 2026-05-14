"""Unmapped SKUs report.

Lists every distinct OrderLine.sku that does NOT appear in the catalog
(neither in the SKU master nor the bundle mapping). Use this to figure out
which TikTok SKU IDs need to be added to the Master SKU Sheet so COGS and
product names flow through.

Each row shows: the identifier, total units, total gross sales, and the
first/last time it appeared on a paid order.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class UnmappedRow:
    identifier: str          # whatever OrderLine.sku stored (usually a TikTok SKU ID)
    units: int
    gross: Decimal
    line_count: int
    first_seen: datetime
    last_seen: datetime


def find_unmapped_skus(db: Session) -> list[UnmappedRow]:
    # Collect every catalog key.
    catalog_keys: set[str] = set()
    for s in db.execute(select(Sku)).scalars():
        for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if k:
                catalog_keys.add(str(k).strip())
    for b in db.execute(select(Bundle)).scalars():
        for k in (b.tiktok_sku_id, b.bundle_sku):
            if k:
                catalog_keys.add(str(k).strip())

    stmt = (
        select(
            OrderLine.sku,
            func.sum(OrderLine.quantity).label("units"),
            func.sum(OrderLine.gross_sales).label("gross"),
            func.count(OrderLine.id).label("lines"),
            func.min(Order.placed_at).label("first"),
            func.max(Order.placed_at).label("last"),
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .group_by(OrderLine.sku)
        .order_by(func.sum(OrderLine.quantity).desc())
    )

    out: list[UnmappedRow] = []
    for r in db.execute(stmt):
        key = (r.sku or "").strip()
        if not key or key in catalog_keys:
            continue
        out.append(UnmappedRow(
            identifier=key,
            units=int(r.units or 0),
            gross=Decimal(str(r.gross or 0)),
            line_count=int(r.lines or 0),
            first_seen=r.first,
            last_seen=r.last,
        ))
    return out
