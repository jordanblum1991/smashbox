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

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
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
    tiktok_fees: Decimal                       # rolled-up sum of the 8 below
    tiktok_referral_fee: Decimal
    tiktok_transaction_fee: Decimal
    tiktok_refund_admin_fee: Decimal
    tiktok_sales_tax_on_referral: Decimal
    tiktok_smart_promo_fee: Decimal
    tiktok_campaign_fees: Decimal
    tiktok_partner_commission: Decimal
    tiktok_managed_service: Decimal
    affiliate_commission: Decimal
    shop_ads_cost: Decimal
    gmv_max_ad_spend: Decimal                  # TikTok Ads (GMV Max) — from Cost export
    ad_credit_offset: Decimal                  # Manually-entered ad credits for the month
    shipping_revenue: Decimal
    shipping_cost: Decimal
    net_profit: Decimal

    # Settlement coverage — what fraction of paid orders in this month have
    # been settled by TikTok (and therefore have fees / shipping / etc).
    # Pending orders still contribute gross sales and discount lines but
    # contribute $0 to the cost lines, so coverage < 100% means costs are
    # understated and net profit is overstated.
    orders_count: int = 0
    orders_settled: int = 0

    # Volume metrics for the operating-metrics tiles on the Dashboard.
    # `units_sold` is SUM(OrderLine.quantity) for PAID orders only — bundles
    # naturally count as 1 line (one OrderLine per bundle, qty unit). We do
    # NOT explode bundles into components here.
    units_sold: int = 0

    @property
    def settlement_coverage_pct(self) -> Decimal:
        if self.orders_count == 0:
            return Decimal("0")
        return (Decimal(self.orders_settled) / Decimal(self.orders_count)) * 100

    @property
    def aov_after_discounts(self) -> Decimal:
        """Average order value AFTER both TikTok-funded and seller-funded
        discounts (i.e. Net Customer Sales / orders_count)."""
        if self.orders_count == 0:
            return Decimal("0")
        return self.net_customer_sales / Decimal(self.orders_count)

    # Convenience aggregate for reconciliation against TikTok's reported total.
    @property
    def seller_funded_total(self) -> Decimal:
        return self.outlandish_discount + self.smashbox_discount

    @property
    def sales_pre_refund(self) -> Decimal:
        """Sales BEFORE the refund deduction — matches the headline "Sales"
        figure on TikTok Seller Center's dashboard. Net Customer Sales is the
        accounting-correct version (revenue net of returns per ASC 606); this
        sibling exists so finance can tie our numbers to what TikTok shows.

        Mathematically: gross_sales − all discounts (no refund subtraction),
        equivalently net_customer_sales + refunds.
        """
        return self.net_customer_sales + self.refunds

    @property
    def gross_margin(self) -> Decimal:
        if self.net_customer_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.net_customer_sales

    @property
    def total_operating_expenses(self) -> Decimal:
        """Everything between Gross Profit and Net Profit — TikTok fees,
        affiliate, ads (net of credits), and net shipping. Defined as the
        gap rather than re-summed so the math stays self-consistent with
        the net_profit calculation."""
        return self.gross_profit - self.net_profit

    @property
    def net_margin(self) -> Decimal:
        if self.net_customer_sales == 0:
            return Decimal("0")
        return self.net_profit / self.net_customer_sales

    @property
    def total_ad_spend(self) -> Decimal:
        """GROSS paid marketing in the period (before manual ad credits):
        settlement-reported Shop Ads + TikTok Ads Manager GMV Max."""
        return self.shop_ads_cost + self.gmv_max_ad_spend

    @property
    def net_ad_spend(self) -> Decimal:
        """Gross ad spend minus manually-entered TikTok ad credits. This is
        the true cash cost of marketing — what flows into the P&L and what
        ROAS is computed against."""
        return self.total_ad_spend - self.ad_credit_offset

    @property
    def roas(self) -> Decimal:
        """Return on Ad Spend: $ of Net Customer Sales generated per $1 of
        NET ad spend (after applying ad credits). 0 when no net spend
        (avoids divide-by-zero)."""
        if self.net_ad_spend <= 0:
            return Decimal("0")
        return self.net_customer_sales / self.net_ad_spend


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
            func.coalesce(func.sum(Order.tiktok_referral_fee), 0).label("tiktok_referral_fee"),
            func.coalesce(func.sum(Order.tiktok_transaction_fee), 0).label("tiktok_transaction_fee"),
            func.coalesce(func.sum(Order.tiktok_refund_admin_fee), 0).label("tiktok_refund_admin_fee"),
            func.coalesce(func.sum(Order.tiktok_sales_tax_on_referral), 0).label("tiktok_sales_tax_on_referral"),
            func.coalesce(func.sum(Order.tiktok_smart_promo_fee), 0).label("tiktok_smart_promo_fee"),
            func.coalesce(func.sum(Order.tiktok_campaign_fees), 0).label("tiktok_campaign_fees"),
            func.coalesce(func.sum(Order.tiktok_partner_commission), 0).label("tiktok_partner_commission"),
            func.coalesce(func.sum(Order.tiktok_managed_service), 0).label("tiktok_managed_service"),
            func.coalesce(func.sum(Order.affiliate_commission), 0).label("affiliate_commission"),
            func.coalesce(func.sum(Order.shop_ads_cost), 0).label("shop_ads_cost"),
            func.coalesce(func.sum(Order.shipping_revenue), 0).label("ship_rev"),
            func.coalesce(func.sum(Order.shipping_cost), 0).label("ship_cost"),
            func.count(Order.id).label("orders_count"),
            # Settlement back-fill writes tiktok_fees > 0 (every settled order
            # has at least a referral fee). Use that as the settled flag.
            func.sum(case((Order.tiktok_fees > 0, 1), else_=0)).label("orders_settled"),
        )
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).one()

    cogs = _paid_cogs(db, start, end)

    gmv_max_ad_spend = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(AdSpend.amount), 0))
            .where(AdSpend.spend_date >= start, AdSpend.spend_date < end)
        ).scalar() or 0
    ))

    ad_credit_offset = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(AdCredit.amount), 0))
            .where(AdCredit.year == year, AdCredit.month == month)
        ).scalar() or 0
    ))

    # Units sold (paid orders only). Bundles are one OrderLine each, so this
    # naturally counts a bundle as a single item — not its components.
    units_sold = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
    ).scalar() or 0

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

    net_profit = (
        gross_profit
        - tiktok_fees
        - affiliate
        - shop_ads
        - gmv_max_ad_spend
        + ad_credit_offset                # credits reduce ad expense
        - ship_cost
        + ship_rev
    )

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
        tiktok_referral_fee=Decimal(str(row.tiktok_referral_fee)),
        tiktok_transaction_fee=Decimal(str(row.tiktok_transaction_fee)),
        tiktok_refund_admin_fee=Decimal(str(row.tiktok_refund_admin_fee)),
        tiktok_sales_tax_on_referral=Decimal(str(row.tiktok_sales_tax_on_referral)),
        tiktok_smart_promo_fee=Decimal(str(row.tiktok_smart_promo_fee)),
        tiktok_campaign_fees=Decimal(str(row.tiktok_campaign_fees)),
        tiktok_partner_commission=Decimal(str(row.tiktok_partner_commission)),
        tiktok_managed_service=Decimal(str(row.tiktok_managed_service)),
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        gmv_max_ad_spend=gmv_max_ad_spend,
        ad_credit_offset=ad_credit_offset,
        shipping_revenue=ship_rev,
        shipping_cost=ship_cost,
        net_profit=net_profit,
        orders_count=int(row.orders_count or 0),
        orders_settled=int(row.orders_settled or 0),
        units_sold=int(units_sold),
    )


def _paid_cogs(db: Session, start: datetime, end: datetime) -> Decimal:
    """Sum qty * unit_cogs_snapshot for paid orders. Falls back to SKU master
    COGS when the snapshot is zero (legacy rows imported before COGS was set)."""
    # OrderLine.sku holds the TikTok SKU ID after resolution. Fallback joins
    # against Sku.tiktok_sku_id for SKUs that exist in the master but somehow
    # missed snapshotting. Bundles are not in this fallback — they always have
    # a populated snapshot from the resolver.
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
        .join(Sku, Sku.tiktok_sku_id == OrderLine.sku, isouter=True)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
    )
    return Decimal(str(db.execute(stmt).scalar() or 0))


def _add_month(d: datetime) -> datetime:
    if d.month == 12:
        return datetime(d.year + 1, 1, 1)
    return datetime(d.year, d.month + 1, 1)
