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
        tiktok_referral_fee=s("tiktok_referral_fee"),
        tiktok_transaction_fee=s("tiktok_transaction_fee"),
        tiktok_refund_admin_fee=s("tiktok_refund_admin_fee"),
        tiktok_sales_tax_on_referral=s("tiktok_sales_tax_on_referral"),
        tiktok_smart_promo_fee=s("tiktok_smart_promo_fee"),
        tiktok_campaign_fees=s("tiktok_campaign_fees"),
        tiktok_partner_commission=s("tiktok_partner_commission"),
        tiktok_managed_service=s("tiktok_managed_service"),
        affiliate_commission=s("affiliate_commission"),
        shop_ads_cost=s("shop_ads_cost"),
        gmv_max_ad_spend=s("gmv_max_ad_spend"),
        ad_credit_offset=s("ad_credit_offset"),
        shipping_revenue=s("shipping_revenue"),
        shipping_cost=s("shipping_cost"),
        net_profit=s("net_profit"),
        orders_count=sum((m.orders_count for m in months), 0),
        orders_settled=sum((m.orders_settled for m in months), 0),
        units_sold=sum((m.units_sold for m in months), 0),
    )
