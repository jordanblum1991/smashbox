"""Monthly P&L.

Reads paid orders within a [month_start, next_month) window, aggregates the
canonical line items, and returns a PnL dataclass. Free samples are excluded
from revenue but their COGS lands in the sample-tracking report, not here.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class MonthlyPnL:
    month: date
    gross_sales: Decimal
    refunds: Decimal
    net_sales: Decimal
    tiktok_fees: Decimal
    affiliate_commission: Decimal
    shop_ads_cost: Decimal
    seller_funded_total: Decimal
    seller_funded_outlandish: Decimal
    seller_funded_smashbox: Decimal
    shipping_revenue: Decimal
    shipping_cost: Decimal
    cogs: Decimal
    gross_profit: Decimal
    net_profit: Decimal

    @property
    def gross_margin(self) -> Decimal:
        if self.net_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.net_sales


def compute_monthly_pnl(db: Session, year: int, month: int) -> MonthlyPnL:
    start = datetime(year, month, 1)
    end = _add_month(start)

    paid = (
        select(
            func.coalesce(func.sum(Order.gross_sales), 0).label("gross_sales"),
            func.coalesce(func.sum(Order.refunds), 0).label("refunds"),
            func.coalesce(func.sum(Order.tiktok_fees), 0).label("tiktok_fees"),
            func.coalesce(func.sum(Order.affiliate_commission), 0).label("affiliate_commission"),
            func.coalesce(func.sum(Order.shop_ads_cost), 0).label("shop_ads_cost"),
            func.coalesce(func.sum(Order.seller_funded_discount_total), 0).label("sf_total"),
            func.coalesce(func.sum(Order.seller_funded_outlandish), 0).label("sf_out"),
            func.coalesce(func.sum(Order.seller_funded_smashbox), 0).label("sf_smash"),
            func.coalesce(func.sum(Order.shipping_revenue), 0).label("ship_rev"),
            func.coalesce(func.sum(Order.shipping_cost), 0).label("ship_cost"),
        )
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    )
    row = db.execute(paid).one()

    cogs = _paid_cogs(db, start, end)

    gross_sales = Decimal(str(row.gross_sales))
    refunds = Decimal(str(row.refunds))
    net_sales = gross_sales - refunds - Decimal(str(row.sf_total))
    gross_profit = net_sales - cogs
    net_profit = (
        gross_profit
        - Decimal(str(row.tiktok_fees))
        - Decimal(str(row.affiliate_commission))
        - Decimal(str(row.shop_ads_cost))
        - Decimal(str(row.ship_cost))
        + Decimal(str(row.ship_rev))
    )

    return MonthlyPnL(
        month=start.date(),
        gross_sales=gross_sales,
        refunds=refunds,
        net_sales=net_sales,
        tiktok_fees=Decimal(str(row.tiktok_fees)),
        affiliate_commission=Decimal(str(row.affiliate_commission)),
        shop_ads_cost=Decimal(str(row.shop_ads_cost)),
        seller_funded_total=Decimal(str(row.sf_total)),
        seller_funded_outlandish=Decimal(str(row.sf_out)),
        seller_funded_smashbox=Decimal(str(row.sf_smash)),
        shipping_revenue=Decimal(str(row.ship_rev)),
        shipping_cost=Decimal(str(row.ship_cost)),
        cogs=cogs,
        gross_profit=gross_profit,
        net_profit=net_profit,
    )


def _paid_cogs(db: Session, start: datetime, end: datetime) -> Decimal:
    """Sum qty * unit_cogs_snapshot for paid orders. Falls back to SKU master COGS
    when the snapshot is zero (e.g. legacy rows imported before COGS was set)."""
    stmt = (
        select(
            func.coalesce(
                func.sum(
                    OrderLine.quantity
                    * func.coalesce(
                        func.nullif(OrderLine.unit_cogs_snapshot, 0),
                        func.coalesce(Sku.unit_cogs, 0),
                    )
                ),
                0,
            )
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .join(Sku, Sku.sku == OrderLine.sku, isouter=True)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
    )
    return Decimal(str(db.execute(stmt).scalar() or 0))


def _add_month(d: datetime) -> datetime:
    if d.month == 12:
        return datetime(d.year + 1, 1, 1)
    return datetime(d.year, d.month + 1, 1)
