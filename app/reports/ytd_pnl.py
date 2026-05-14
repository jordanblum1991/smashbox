"""YTD P&L — sums monthly P&Ls Jan..current_month for a given year."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.reports.monthly_pnl import MonthlyPnL, compute_monthly_pnl


@dataclass
class YtdPnL:
    year: int
    months: list[MonthlyPnL]
    total: MonthlyPnL


def compute_ytd_pnl(db: Session, year: int, through_month: int | None = None) -> YtdPnL:
    last = through_month or date.today().month
    months = [compute_monthly_pnl(db, year, m) for m in range(1, last + 1)]
    return YtdPnL(year=year, months=months, total=_sum(months, year))


def _sum(months: list[MonthlyPnL], year: int) -> MonthlyPnL:
    zero = Decimal("0")

    def s(attr: str) -> Decimal:
        return sum((getattr(m, attr) for m in months), zero)

    return MonthlyPnL(
        month=date(year, 1, 1),
        gross_sales=s("gross_sales"),
        platform_discount=s("platform_discount"),
        outlandish_discount=s("outlandish_discount"),
        smashbox_discount=s("smashbox_discount"),
        refunds=s("refunds"),
        net_customer_sales=s("net_customer_sales"),
        cogs=s("cogs"),
        gross_profit=s("gross_profit"),
        tiktok_fees=s("tiktok_fees"),
        affiliate_commission=s("affiliate_commission"),
        shop_ads_cost=s("shop_ads_cost"),
        shipping_revenue=s("shipping_revenue"),
        shipping_cost=s("shipping_cost"),
        net_profit=s("net_profit"),
        orders_count=sum((m.orders_count for m in months), 0),
        orders_settled=sum((m.orders_settled for m in months), 0),
    )
