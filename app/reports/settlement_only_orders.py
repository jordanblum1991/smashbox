"""Settlement-only orders report.

Lists TikTok order IDs that appear in the settlement file but have no matching
Order row in our database. These are orders we know exist (TikTok already paid
or fee'd them) but for which we don't have the line-level data: SKUs, gross
sales, the seller-funded discount split, etc.

Cause is almost always: the orders-file date range doesn't extend back as far
as the settlement file. Fix is to export a wider window from TikTok Seller
Center and re-upload the orders file.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.settlement import Settlement


@dataclass
class OrphanRow:
    tiktok_order_id: str
    statement_ids: int            # how many distinct statements referenced this order
    settlement_gross: Decimal
    settlement_fees: Decimal
    paid_date: datetime | None
    settled_date: datetime | None


def find_settlement_only_orders(db: Session) -> list[OrphanRow]:
    """Settlement.tiktok_order_id values with no matching Order.

    Aggregated to one row per order_id (an order can appear in multiple
    statements — refund issued in a later statement, etc.).
    """
    stmt = (
        select(
            Settlement.tiktok_order_id.label("oid"),
            func.count(distinct(Settlement.linked_statement_id)).label("statements"),
            func.coalesce(func.sum(Settlement.gross_sales), 0).label("gross"),
            func.coalesce(func.sum(Settlement.tiktok_fees), 0).label("fees"),
            func.min(Settlement.paid_date).label("paid"),
            func.min(Settlement.settled_date).label("settled"),
        )
        .outerjoin(Order, Order.tiktok_order_id == Settlement.tiktok_order_id)
        .where(Order.id.is_(None))
        .group_by(Settlement.tiktok_order_id)
        .order_by(func.coalesce(func.sum(Settlement.gross_sales), 0).desc())
    )
    return [
        OrphanRow(
            tiktok_order_id=r.oid,
            statement_ids=int(r.statements or 0),
            settlement_gross=Decimal(str(r.gross or 0)),
            settlement_fees=Decimal(str(r.fees or 0)),
            paid_date=r.paid,
            settled_date=r.settled,
        )
        for r in db.execute(stmt)
    ]


def count_settlement_only_orders(db: Session) -> int:
    return db.execute(
        select(func.count(distinct(Settlement.tiktok_order_id)))
        .outerjoin(Order, Order.tiktok_order_id == Settlement.tiktok_order_id)
        .where(Order.id.is_(None))
    ).scalar() or 0
