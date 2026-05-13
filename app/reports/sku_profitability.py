"""SKU-level profitability.

Bundles are exploded: a sold bundle SKU contributes to its component SKUs using
either the per-component `revenue_allocation_pct` (if set) or a fallback split
weighted by quantity * unit COGS.

TODO: implement bundle explosion when the bundle mapping importer lands. For
now this reports only on physical SKUs sold directly.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class SkuRow:
    sku: str
    name: str | None
    units_sold: int
    gross_sales: Decimal
    cogs: Decimal
    gross_profit: Decimal

    @property
    def gross_margin(self) -> Decimal:
        if self.gross_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.gross_sales


def compute_sku_profitability(db: Session, start: datetime, end: datetime) -> list[SkuRow]:
    stmt = (
        select(
            OrderLine.sku,
            Sku.name,
            func.coalesce(func.sum(OrderLine.quantity), 0).label("units"),
            func.coalesce(func.sum(OrderLine.gross_sales), 0).label("gross"),
            func.coalesce(
                func.sum(
                    OrderLine.quantity
                    * func.coalesce(
                        func.nullif(OrderLine.unit_cogs_snapshot, 0),
                        func.coalesce(Sku.unit_cogs, 0),
                    )
                ),
                0,
            ).label("cogs"),
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .join(Sku, Sku.sku == OrderLine.sku, isouter=True)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .group_by(OrderLine.sku, Sku.name)
        .order_by(func.sum(OrderLine.gross_sales).desc())
    )
    rows: list[SkuRow] = []
    for r in db.execute(stmt):
        gross = Decimal(str(r.gross))
        cogs = Decimal(str(r.cogs))
        rows.append(SkuRow(
            sku=r.sku,
            name=r.name,
            units_sold=int(r.units),
            gross_sales=gross,
            cogs=cogs,
            gross_profit=gross - cogs,
        ))
    return rows
