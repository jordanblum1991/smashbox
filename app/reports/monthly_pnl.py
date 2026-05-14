"""Monthly P&L.

Aggregates PAID orders in the [month_start, next_month) window. Free samples
are excluded from revenue; their COGS lands in the sample-tracking report.

The discount section is presented as a waterfall — every line a separate
deduction so a reader can see exactly who funded what:

  Gross Product Sales
  − TikTok-Funded Discount    (TikTok promo; not our cost)
  − Outlandish-Funded Discount (first 10% of post-TikTok price)
  − Smashbox-Funded Discount   (residual seller-funded)
  − Refunds
  = Net Customer Sales         (a.k.a. Net Product Revenue)
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class MonthlyPnL:
    month: date

    # Revenue waterfall
    gross_sales: Decimal
    platform_discount: Decimal
    outlandish_discount: Decimal
    smashbox_discount: Decimal
    refunds: Decimal
    net_customer_sales: Decimal

    # Cost lines
    cogs: Decimal
    gross_profit: Decimal
    tiktok_fees: Decimal
    affiliate_commission: Decimal
    shop_ads_cost: Decimal
    shipping_revenue: Decimal
    shipping_cost: Decimal
    net_profit: Decimal

    # Convenience aggregate for reconciliation against TikTok's reported total.
    @property
    def seller_funded_total(self) -> Decimal:
        return self.outlandish_discount + self.smashbox_discount

    @property
    def gross_margin(self) -> Decimal:
        if self.net_customer_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.net_customer_sales


def compute_monthly_pnl(db: Session, year: int, month: int) -> MonthlyPnL:
    start = datetime(year, month, 1)
    end = _add_month(start)

    row = db.execute(
        select(
            func.coalesce(func.sum(Order.gross_sales), 0).label("gross_sales"),
            func.coalesce(func.sum(Order.platform_discount_total), 0).label("platform_disc"),
            func.coalesce(func.sum(Order.seller_funded_outlandish), 0).label("outlandish"),
            func.coalesce(func.sum(Order.seller_funded_smashbox), 0).label("smashbox"),
            func.coalesce(func.sum(Order.refunds), 0).label("refunds"),
            func.coalesce(func.sum(Order.tiktok_fees), 0).label("tiktok_fees"),
            func.coalesce(func.sum(Order.affiliate_commission), 0).label("affiliate_commission"),
            func.coalesce(func.sum(Order.shop_ads_cost), 0).label("shop_ads_cost"),
            func.coalesce(func.sum(Order.shipping_revenue), 0).label("ship_rev"),
            func.coalesce(func.sum(Order.shipping_cost), 0).label("ship_cost"),
        )
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).one()

    cogs = _paid_cogs(db, start, end)

    gross_sales = Decimal(str(row.gross_sales))
    platform_disc = Decimal(str(row.platform_disc))
    outlandish = Decimal(str(row.outlandish))
    smashbox = Decimal(str(row.smashbox))
    refunds = Decimal(str(row.refunds))

    net_customer_sales = gross_sales - platform_disc - outlandish - smashbox - refunds
    gross_profit = net_customer_sales - cogs
    tiktok_fees = Decimal(str(row.tiktok_fees))
    affiliate = Decimal(str(row.affiliate_commission))
    shop_ads = Decimal(str(row.shop_ads_cost))
    ship_rev = Decimal(str(row.ship_rev))
    ship_cost = Decimal(str(row.ship_cost))

    net_profit = gross_profit - tiktok_fees - affiliate - shop_ads - ship_cost + ship_rev

    return MonthlyPnL(
        month=start.date(),
        gross_sales=gross_sales,
        platform_discount=platform_disc,
        outlandish_discount=outlandish,
        smashbox_discount=smashbox,
        refunds=refunds,
        net_customer_sales=net_customer_sales,
        cogs=cogs,
        gross_profit=gross_profit,
        tiktok_fees=tiktok_fees,
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        shipping_revenue=ship_rev,
        shipping_cost=ship_cost,
        net_profit=net_profit,
    )


def _paid_cogs(db: Session, start: datetime, end: datetime) -> Decimal:
    """Sum qty * unit_cogs_snapshot for paid orders. Falls back to SKU master
    COGS when the snapshot is zero (legacy rows imported before COGS was set)."""
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
