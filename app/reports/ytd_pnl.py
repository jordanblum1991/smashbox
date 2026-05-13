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
    total = _sum(months, year)
    return YtdPnL(year=year, months=months, total=total)


def _sum(months: list[MonthlyPnL], year: int) -> MonthlyPnL:
    zero = Decimal("0")

    def s(attr: str) -> Decimal:
        return sum((getattr(m, attr) for m in months), zero)

    return MonthlyPnL(
        month=date(year, 1, 1),
        gross_sales=s("gross_sales"),
        refunds=s("refunds"),
        net_sales=s("net_sales"),
        tiktok_fees=s("tiktok_fees"),
        affiliate_commission=s("affiliate_commission"),
        shop_ads_cost=s("shop_ads_cost"),
        seller_funded_total=s("seller_funded_total"),
        seller_funded_outlandish=s("seller_funded_outlandish"),
        seller_funded_smashbox=s("seller_funded_smashbox"),
        shipping_revenue=s("shipping_revenue"),
        shipping_cost=s("shipping_cost"),
        cogs=s("cogs"),
        gross_profit=s("gross_profit"),
        net_profit=s("net_profit"),
    )
